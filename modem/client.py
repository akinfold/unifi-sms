"""Serialize modem AT commands through one locked SSH session."""

import csv
import re
import shlex
import time
from io import StringIO

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
old=
cleanup() {{
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
    case "$command" in
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

    def _read_line(self, deadline: float, budget: list[int] | None = None) -> str:
        channel = self._channel
        if channel is None:
            raise ModemError("Modem session is closed")
        while b"\n" not in self._buffer:
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
                raise ModemError(line)
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
