"""Serialize modem AT commands through one locked SSH session."""

import csv
import hashlib
import re
import shlex
import time
from contextlib import ExitStack
from io import StringIO

from .balance import balance_pdu, parse_cusd, ussd_code
from .outbound import OutboundError, OutboundSimChanged, OutboundUnknown, submit_pdu
from .qmi import open_wms
from .ssh import SSHCommandError, command_deadline, read_command


MODEM_DEVICE = "/dev/ttyUSB2"
SMS_STORAGE = "SM"

_BEGIN = "__UNIFI_SMS_BEGIN__"
_READY = "__UNIFI_SMS_READY__"
_MAX_RESPONSE = 256 * 1024
_HEX = re.compile(r"^[0-9A-Fa-f]+$")
_LIST_HEADER = re.compile(r"^\+CMGL:\s*(\d+)\s*,")
_READ_HEADER = re.compile(r"^\+CMGR:\s*")


class ModemError(RuntimeError):
    """An SSH transport or modem command failed."""


class ATRejected(ModemError):
    """The modem returned a final error result."""


def parse_private_key(value):
    """Parse an unencrypted SSH private key without exposing parser diagnostics."""
    import paramiko

    for key_type in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return key_type.from_private_key(StringIO(value))
        except Exception:
            continue
    raise ValueError("Invalid SSH private key")


def parse_host_keys(value):
    """Parse OpenSSH known_hosts lines for strict host verification."""
    import paramiko

    keys = paramiko.HostKeys()
    count = 0
    for line in value.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
            if entry is None or not entry.hostnames or entry.key is None:
                raise ValueError
            for hostname in entry.hostnames:
                keys.add(hostname, entry.key.get_name(), entry.key)
            count += 1
        except Exception:
            raise ValueError("Invalid SSH host keys") from None
    if not count:
        raise ValueError("Invalid SSH host keys")
    return keys


class ModemClient:
    """Keep the modem lock across reads and deletions in one SSH context.

    Authentication uses only the selected credential. The host key must match
    supplied known_hosts data. A failed command is never retried automatically.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        key_filename: str,
        known_hosts: str,
        device: str = MODEM_DEVICE,
        storage: str = SMS_STORAGE,
        timeout: float = 15,
        *,
        auth_method: str = "key_file",
        password: str = "",
        private_key: str = "",
        host_keys: str = "",
    ):
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("Invalid SSH port")
        if storage not in ("SM", "ME"):
            raise ValueError("Storage must be SM or ME")
        if not isinstance(device, str) or re.fullmatch(r"/dev/[A-Za-z0-9._/-]+", device) is None:
            raise ValueError("Device must be an absolute /dev path")
        if timeout <= 0:
            raise ValueError("Timeout must be positive")
        if auth_method not in ("key_file", "private_key", "password"):
            raise ValueError("Invalid SSH authentication method")
        if auth_method == "password" and not password:
            raise ValueError("Missing SSH password")
        if auth_method == "private_key" and not private_key:
            raise ValueError("Missing SSH private key")
        if auth_method == "key_file" and not key_filename:
            raise ValueError("Missing SSH key file")
        if not host_keys and not known_hosts:
            raise ValueError("Missing SSH host keys")
        self.host, self.port, self.username = host, port, username
        self.key_filename, self.known_hosts = key_filename, known_hosts
        self.device, self.storage, self.timeout = device, storage, timeout
        self.auth_method, self.password = auth_method, password
        self.private_key, self.host_keys = private_key, host_keys
        self._ssh = None
        self._channel = None
        self._buffer = b""
        self._original_mode = None

    @staticmethod
    def _slot(slot: int) -> int:
        if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
            raise ValueError("Slot must be a nonnegative integer")
        return slot

    def __enter__(self):
        import paramiko

        ssh = paramiko.SSHClient()
        self._ssh = ssh
        try:
            if self.host_keys:
                inline_keys = parse_host_keys(self.host_keys)
                for hostname, keytypes in inline_keys.items():
                    for keytype, key in keytypes.items():
                        ssh.get_host_keys().add(hostname, keytype, key)
            else:
                ssh.load_host_keys(self.known_hosts)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            authentication = (
                {"password": self.password}
                if self.auth_method == "password"
                else {"pkey": parse_private_key(self.private_key)}
                if self.auth_method == "private_key"
                else {"key_filename": self.key_filename}
            )
            ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                look_for_keys=False,
                allow_agent=False,
                timeout=self.timeout,
                auth_timeout=self.timeout,
                banner_timeout=self.timeout,
                **authentication,
            )
            with command_deadline(ssh, self.timeout) as deadline:
                channel = ssh.get_transport().open_session(
                    timeout=max(0.001, deadline - time.monotonic())
                )
                self._channel = channel
                channel.settimeout(max(0.001, deadline - time.monotonic()))
                channel.set_combine_stderr(True)
                channel.exec_command(self._remote_script())
                while self._read_line(deadline) != _READY:
                    pass
            lines = self._request("+CMGF?")
            modes = [re.fullmatch(r"\+CMGF:\s*([01])", line) for line in lines]
            self._original_mode = next((int(match[1]) for match in modes if match), None)
            if self._original_mode is None:
                raise ModemError("Missing modem message mode")
            self._request("+CMGF=0")
            self._request(f'+CPMS="{self.storage}"')
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        channel = self._channel
        self._channel = None
        if channel is not None:
            try:
                if self._original_mode == 1 and not channel.closed:
                    self._channel = channel
                    self._request("+CMGF=1")
                    self._channel = None
            except Exception:
                self._channel = None
            try:
                if not channel.closed:
                    channel.sendall("EXIT\n")
                    channel.shutdown_write()
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        channel.settimeout(max(0.01, deadline - time.monotonic()))
                        if not channel.recv(4096):
                            break
            except Exception:
                pass
            finally:
                channel.close()
        if self._ssh is not None:
            self._ssh.close()
            self._ssh = None
        self._buffer = b""
        self._original_mode = None

    def _remote_script(self) -> str:
        device = shlex.quote(self.device)
        storage = self.storage
        lock_attempts = max(1, min(30, int(self.timeout) - 2))
        return f'''exec 2>&1
umask 077
device={device}
lock=/tmp/unifi-sms-modem.lock
n=0
while ! mkdir "$lock" 2>/dev/null; do
    n=$((n + 1))
    if [ "$n" -ge {lock_attempts} ]; then echo LOCK_TIMEOUT; exit 1; fi
    sleep 1
done
reader=
prompt=
old=
cleanup() {{
    if [ -n "$prompt" ]; then printf '\\033' > "$device"; fi
    if [ -n "$reader" ]; then kill "$reader" 2>/dev/null; wait "$reader" 2>/dev/null; fi
    if [ -n "$old" ]; then stty -F "$device" "$old" 2>/dev/null; fi
    rmdir "$lock" 2>/dev/null
}}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
old=$(stty -F "$device" -g) || exit 1
stty -F "$device" raw -echo || exit 1
cat "$device" &
reader=$!
printf '{_READY}\\n'
while IFS= read -r command; do
    if [ "$command" = EXIT ]; then break; fi
    if [ -n "$prompt" ]; then
        case "$command" in
            DATA:*)
                hex=${{command#DATA:}}
                case "$hex" in ''|*[!0-9A-F]*) exit 1;; esac
                [ "${{#hex}}" -le 400 ] || exit 1
                printf '%s\\032' "$hex" > "$device" || exit 1
                prompt=
                continue ;;
            *) printf '\\033' > "$device"; prompt= ;;
        esac
    fi
    case "$command" in
        '+CCID'|'+CIMI'|'+CUSD=?'|'+CUSD?'|'+CUSD=0'|'+CUSD=1'|'+CUSD=2'|'+CRSM=176,12258,0,0,10'|'+CSCA?'|'+CMGS=?') ;;
        '+CUSD=1,"'*'",15')
            code=${{command#?????????}}
            code=${{code%????}}
            case "$code" in ''|*[!0-9*#]*) exit 1;; esac
            [ "${{#code}}" -le 32 ] || exit 1 ;;
        +CMGS=*)
            number=${{command#*=}}
            case "$number" in ''|*[!0-9]*) exit 1;; esac
            prompt=1 ;;
        '+CNUM'|'+CMGF?'|'+CMGF=0'|'+CMGF=1'|'+CPMS="{storage}"'|'+CMGL=4') ;;
        +CMGR=*|+CMGD=*)
            number=${{command#*=}}
            case "$number" in ''|*[!0-9]*) exit 1;; esac ;;
        *) exit 1;;
    esac
    printf '{_BEGIN}\\n'
    printf 'AT%s\\r' "$command" > "$device" || exit 1
done
'''

    def _read_line(
        self, deadline: float, budget: list[int] | None = None, *, prompt=False
    ) -> str:
        channel = self._channel
        if channel is None:
            raise ModemError("Modem session is closed")
        while b"\n" not in self._buffer:
            if prompt and self._buffer.lstrip(b"\r ").startswith(b"> "):
                self._buffer = self._buffer.lstrip(b"\r ")[2:]
                return "> "
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Modem response timed out")
            channel.settimeout(remaining)
            try:
                chunk = channel.recv(4096)
            except TimeoutError as exc:
                raise TimeoutError("Modem response timed out") from exc
            if not chunk:
                raise ModemError("Modem session closed unexpectedly")
            if budget is not None:
                budget[0] += len(chunk)
                if budget[0] > _MAX_RESPONSE:
                    raise ModemError("Modem response exceeds limit")
            self._buffer += chunk
            if len(self._buffer) > _MAX_RESPONSE:
                raise ModemError("Modem response exceeds limit")
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("ascii", errors="replace").strip("\r ")

    def _request(self, command: str) -> list[str]:
        if self._channel is None:
            raise ModemError("Modem session is closed")
        deadline = time.monotonic() + self.timeout
        self._channel.sendall(command + "\n")
        budget = [0]
        while self._read_line(deadline, budget) != _BEGIN:
            pass
        lines = []
        while True:
            line = self._read_line(deadline, budget).strip()
            if not line or line == "AT" + command:
                continue
            if line == "OK":
                return lines
            if line == "ERROR" or line.startswith(("+CMS ERROR:", "+CME ERROR:")):
                raise ATRejected(line)
            lines.append(line)

    def phone_numbers(self) -> list[str]:
        """Read subscriber numbers stored on the SIM; return an empty list if absent."""
        numbers = []
        for line in self._request("+CNUM"):
            if line.startswith("+CMTI:"):
                continue
            if not line.startswith("+CNUM:"):
                raise ModemError("Unexpected CNUM response")
            try:
                fields = next(
                    csv.reader([line.split(":", 1)[1].strip()], strict=True, skipinitialspace=True)
                )
                if len(fields) < 3 or not fields[2].strip().isdigit():
                    raise ValueError
                number = fields[1].strip()
                if not number:
                    continue
                if not re.fullmatch(r"\+?[0-9]{1,20}", number):
                    raise ValueError
                if int(fields[2]) == 145 and not number.startswith("+"):
                    number = "+" + number
                if number not in numbers:
                    numbers.append(number)
            except (csv.Error, ValueError, IndexError) as exc:
                raise ModemError("Invalid CNUM response") from exc
        return numbers

    def _sim_identity(self):
        try:
            lines = self._request("+CCID")
        except ATRejected:
            lines = []
        for line in lines:
            match = re.fullmatch(r'(?:\+CCID:\s*)?"?([0-9]{18,22})[Ff]?"?', line)
            if match:
                return hashlib.sha256(match[1].encode("ascii")).hexdigest()
        # EF_ICCID stores two decimal digits per byte, low nibble first.
        for line in self._request("+CRSM=176,12258,0,0,10"):
            match = re.fullmatch(r'\+CRSM:\s*144,\s*0,\s*"([0-9A-Fa-f]{20})"', line)
            if match:
                digits = "".join(match[1][i : i + 2][::-1] for i in range(0, 20, 2)).rstrip("fF")
                if re.fullmatch(r"[0-9]{18,20}", digits):
                    return hashlib.sha256(digits.encode("ascii")).hexdigest()
        raise OutboundError("Could not identify the SIM.")

    def _sms_center(self):
        for line in self._request("+CSCA?"):
            match = re.fullmatch(r'\+CSCA:\s*"(\+?)([0-9]{1,20})",\s*(129|145|161)', line)
            if match:
                return match[2], 145 if match[1] else int(match[3])
        raise OutboundError("Could not read the SMS center address.")

    def outbound_capabilities(self):
        """Return the SIM fingerprint, candidate profiles, and detected subscriber number."""
        try:
            sim = self._sim_identity()
            try:
                numbers = self.phone_numbers()
            except ATRejected:
                numbers = []
            phone = next((n for n in numbers if re.fullmatch(r"\+[1-9][0-9]{1,14}", n)), "")
            profiles = []
            try:
                self._request("+CMGS=?")
                profiles.append("at_pdu")
            except ATRejected:
                pass
            try:
                self._sms_center()
                with open_wms(self._ssh, self.timeout, self.device) as wms:
                    profiles[:0] = ["qmi_ims_off", "qmi_ims_on"]
                    if not phone:
                        phone = wms.subscriber_number()
            except Exception:
                pass
            return sim, profiles, phone
        except Exception:
            raise OutboundError("Could not inspect the modem and SIM.") from None

    def _check_sim(self, expected_sim):
        if self._sim_identity() != expected_sim:
            raise OutboundSimChanged("The SIM has changed. Test the current SIM before sending.")

    def balance_identity(self) -> tuple[str, str]:
        """Return the SIM fingerprint and a home-network hint without retaining IMSI."""
        try:
            sim = self._sim_identity()
            try:
                lines = self._request("+CIMI")
            except ATRejected:
                lines = []
            # CIMI does not identify the MNC length; five digits are only a hint.
            prefix = next((line[:5] for line in lines if re.fullmatch(r"[0-9]{14,15}", line)), "")
            return sim, prefix
        except Exception:
            raise OutboundError("Could not inspect the modem and SIM.") from None

    def request_balance(self, profile, *, expected_sim, sms_profile=None):
        """Send one carrier request without retrying an uncertain submission."""
        try:
            if self._channel is None or not expected_sim:
                raise OutboundError("A locked modem session and a tested SIM are required.")
            if not isinstance(profile, dict):
                raise ValueError("Invalid balance profile.")
            transport = profile.get("transport")
            if transport in ("ussd", "code"):
                code = ussd_code(profile.get("code"))
                timeout = profile.get("timeout", 30)
                max_timeout = 300 if transport == "code" else 60
                if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                        or not 1 <= timeout <= max_timeout):
                    raise ValueError("Invalid USSD timeout.")
                self._request("+CUSD=?")
                self._check_sim(expected_sim)
                return self._request_ussd(code, min(timeout, 60), sms_reply=transport == "code")
            if transport != "sms":
                raise OutboundError("This balance profile has no modem request.")
            selected = "qmi_ims_off" if sms_profile is None else sms_profile
            if selected not in ("at_pdu", "qmi_ims_off", "qmi_ims_on"):
                raise OutboundError("Unsupported sending profile.")
            pdu = balance_pdu(profile.get("destination"), profile.get("text", ""))
            if selected == "at_pdu":
                self._check_sim(expected_sim)
                self._send_at(pdu)
            else:
                with ExitStack() as stack:
                    try:
                        qmi_pdu = balance_pdu(
                            profile.get("destination"), profile.get("text", ""), self._sms_center()
                        )
                        wms = stack.enter_context(open_wms(self._ssh, self.timeout, self.device))
                    except Exception:
                        if sms_profile is not None:
                            raise
                        wms = None
                    self._check_sim(expected_sim)
                    # Only preparation can choose another route. A send is never replayed.
                    if wms is None:
                        self._send_at(pdu)
                    else:
                        wms.send(qmi_pdu, selected == "qmi_ims_on")
            return None
        except (OutboundError, OutboundUnknown, ValueError):
            raise
        except Exception:
            raise OutboundError("Could not prepare the modem for a balance request.") from None

    def _request_ussd(self, code, timeout, *, sms_reply=False):
        command = f'+CUSD=1,"{code}",15'
        budget = [0]
        deadline = time.monotonic() + timeout
        original = next((line for line in self._request("+CUSD?")
                         if re.fullmatch(r"\+CUSD:\s*[01]", line)), None)
        if original is None:
            raise OutboundError("Could not read the USSD reporting mode.")
        restore = original.rsplit(":", 1)[1].strip() == "0"
        started = False
        accepted = False
        try:
            self._channel.settimeout(timeout)
            # A failed write may still have reached the modem. Never replay it.
            started = True
            self._channel.sendall(command + "\n")
            while self._read_line(deadline, budget) != _BEGIN:
                pass
            result = None
            received = False
            while True:
                line = self._read_line(deadline, budget)
                if line == "OK":
                    accepted = True
                elif line.startswith("+CUSD:"):
                    result = parse_cusd(line)
                    received = True
                elif line == "ERROR" or line.startswith(("+CMS ERROR:", "+CME ERROR:")):
                    raise OutboundError("The modem rejected the USSD request.")
                if accepted and sms_reply:
                    return None
                if accepted and received:
                    return result
        except (OutboundError, OutboundUnknown):
            raise
        except Exception:
            raise OutboundUnknown("The modem did not confirm the USSD result.") from None
        finally:
            if started:
                if not (sms_reply and accepted):
                    try:
                        self._request("+CUSD=2")
                    except Exception:
                        pass
                # Cancelling an acknowledged code could abort the carrier's SMS reply.
                if restore:
                    try:
                        self._request("+CUSD=0")
                    except Exception:
                        pass

    def send_sms(self, profile, phone, text, *, expected_sim):
        """Submit one message after checking the SIM while the modem lock is held."""
        try:
            if self._channel is None or not expected_sim:
                raise OutboundError("A locked modem session and a tested SIM are required.")
            if profile == "at_pdu":
                pdu = submit_pdu(phone, text)
                self._check_sim(expected_sim)
                return self._send_at(pdu)
            if profile not in ("qmi_ims_off", "qmi_ims_on"):
                raise OutboundError("Unsupported sending profile.")
            pdu = submit_pdu(phone, text, self._sms_center())
            with open_wms(self._ssh, self.timeout, self.device) as wms:
                self._check_sim(expected_sim)
                return wms.send(pdu, profile == "qmi_ims_on")
        except (OutboundError, OutboundUnknown):
            raise
        except Exception:
            raise OutboundError("Could not prepare the modem for submission.") from None

    def _send_at(self, pdu):
        deadline = time.monotonic() + self.timeout
        budget = [0]
        command = f"+CMGS={len(pdu) - 1 - pdu[0]}"
        self._channel.settimeout(self.timeout)
        self._channel.sendall(command + "\n")
        while self._read_line(deadline, budget) != _BEGIN:
            pass
        while True:
            line = self._read_line(deadline, budget, prompt=True)
            if line.strip() == ">":
                break
            if line == "ERROR" or line.startswith(("+CMS ERROR:", "+CME ERROR:")):
                raise OutboundError("The modem rejected the SMS prompt.")
        try:
            self._channel.settimeout(self.timeout)
            self._channel.sendall("DATA:" + pdu.hex().upper() + "\n")
            deadline = time.monotonic() + self.timeout
            reference = None
            while True:
                line = self._read_line(deadline, budget)
                match = re.fullmatch(r"\+CMGS:\s*([0-9]{1,3})(?:,\s*[0-9A-Fa-f]+)?", line)
                if match and int(match[1]) <= 255:
                    reference = match[1]
                elif line == "OK":
                    if reference is not None:
                        return reference
                    raise OutboundUnknown("The modem returned no submission reference.")
                elif line == "ERROR" or line.startswith(("+CMS ERROR:", "+CME ERROR:")):
                    if reference is not None or line in ("+CMS ERROR: 332", "+CMS ERROR: 500"):
                        raise OutboundUnknown("The modem could not confirm submission.")
                    raise OutboundError("The modem rejected the message.")
        except (OutboundError, OutboundUnknown):
            raise
        except Exception:
            raise OutboundUnknown("The modem did not confirm submission.") from None

    def device_model(self) -> str:
        if self._ssh is None:
            raise ModemError("Modem session is closed")
        try:
            raw = read_command(
                self._ssh,
                "sed -n '/^board.name=/{s/^board.name=//;p;q;}' /etc/board.info | cut -c 1-128",
                timeout=self.timeout,
            )
        except SSHCommandError:
            raise ModemError("Could not read device model") from None
        try:
            model = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ModemError("Invalid device model") from exc
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}", model) is None:
            raise ModemError("Invalid device model")
        return model

    def list_pdus(self) -> dict[int, str]:
        """Return every stored PDU, keyed by its modem slot."""
        lines = self._request("+CMGL=4")
        found = {}
        slot = None
        for line in lines:
            header = _LIST_HEADER.match(line)
            if header:
                if slot is not None:
                    raise ModemError("Missing PDU after CMGL header")
                slot = int(header[1])
            elif slot is not None and _HEX.fullmatch(line) and len(line) % 2 == 0:
                if slot in found:
                    raise ModemError("Duplicate modem slot")
                found[slot] = line.upper()
                slot = None
            elif not line.startswith("+CMTI:"):
                raise ModemError("Unexpected CMGL response")
        if slot is not None:
            raise ModemError("Missing PDU after CMGL header")
        return found

    def read_pdu(self, slot: int) -> str | None:
        """Read one slot; return None only when the modem reports it missing."""
        slot = self._slot(slot)
        try:
            lines = self._request(f"+CMGR={slot}")
        except ModemError as exc:
            if str(exc).strip() == "+CMS ERROR: 321":
                return None
            raise
        header = False
        pdu = None
        for line in lines:
            if _READ_HEADER.match(line):
                header = True
            elif header and _HEX.fullmatch(line) and len(line) % 2 == 0:
                if pdu is not None:
                    raise ModemError("Multiple PDUs in CMGR response")
                pdu = line.upper()
        if not header or pdu is None:
            raise ModemError("Missing PDU in CMGR response")
        return pdu

    def delete(self, slot: int) -> None:
        """Delete one validated slot once; an ambiguous result is not retried."""
        slot = self._slot(slot)
        self._request(f"+CMGD={slot}")
