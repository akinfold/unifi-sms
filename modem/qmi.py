"""Run WMS requests over the modem host's existing libqmi proxy."""

import contextlib
import re
import shlex
import struct
import time
from pathlib import Path

from .outbound import OutboundError, OutboundUnknown
from .ssh import command_deadline


_BRIDGE = Path(__file__).parent / "qmi_bridge" / "qmi-bridge-mips"
_CTL = 0
_DMS = 2
_WMS = 5
_RAW_SEND = 0x0020
_RESPONSE = {_CTL: 1, _DMS: 2, _WMS: 2}


class ProtocolError(ValueError):
    pass


class Rejected(Exception):
    def __init__(self, code):
        self.code = code


def _tlv(kind, data):
    return struct.pack("<BH", kind, len(data)) + data


def _frame(service, cid, transaction, message, fields):
    layout = "<BBHH" if service == _CTL else "<BHHH"
    body = struct.pack(layout, 0, transaction, message, len(fields)) + fields
    return struct.pack("<BHBBB", 1, 5 + len(body), 0, service, cid) + body


def _fields(raw):
    fields = {}
    while raw:
        if len(raw) < 3:
            raise ProtocolError("Incomplete QMI field")
        kind, length = struct.unpack_from("<BH", raw)
        if length > len(raw) - 3 or kind in fields:
            raise ProtocolError("Invalid QMI field")
        fields[kind] = raw[3 : 3 + length]
        raw = raw[3 + length :]
    return fields


class WMS:
    def __init__(self, channel, timeout):
        self.channel = channel
        self.timeout = timeout
        self.transaction = 0
        self.clients = {}

    def _read(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QMI response timed out")
            self.channel.settimeout(remaining)
            chunk = self.channel.recv(size - len(data))
            if not chunk:
                raise ProtocolError("QMI proxy closed")
            data.extend(chunk)
        return data

    def request(self, service, message, fields=b""):
        self.transaction += 1
        cid = 0 if service == _CTL else self.clients[service]
        deadline = time.monotonic() + self.timeout
        self.channel.settimeout(self.timeout)
        self.channel.sendall(_frame(service, cid, self.transaction, message, fields))
        while True:
            marker, size, direction, src, client = struct.unpack("<BHBBB", self._read(6, deadline))
            if marker != 1 or size < 11:
                raise ProtocolError("Invalid QMI frame")
            body = self._read(size - 5, deadline)
            if (direction, src, client) != (0x80, service, cid):
                continue
            layout = "<BBHH" if service == _CTL else "<BHHH"
            header = struct.calcsize(layout)
            if len(body) < header:
                raise ProtocolError("Incomplete QMI response")
            flags, tx, msg, length = struct.unpack_from(layout, body)
            if (flags, tx, msg) != (_RESPONSE[service], self.transaction, message):
                continue
            if length != len(body) - header:
                raise ProtocolError("Invalid QMI response length")
            fields = _fields(body[header:])
            if len(fields.get(2, b"")) != 4:
                raise ProtocolError("Missing QMI result")
            status, code = struct.unpack("<HH", fields[2])
            if status == 1 and code:
                raise Rejected(code)
            if status or code:
                raise ProtocolError("Invalid QMI result")
            return fields

    def _allocate(self, service):
        allocation = self.request(_CTL, 0x0022, _tlv(1, bytes([service]))).get(1, b"")
        if len(allocation) != 2 or allocation[0] != service or allocation[1] in (0, 255):
            raise ProtocolError("Invalid QMI client ID")
        self.clients[service] = allocation[1]

    def open(self):
        self.request(_CTL, 0xFF00, _tlv(1, b"/dev/cdc-wdm0"))
        self._allocate(_WMS)
        routes = self.request(_WMS, 0x0033).get(1, b"")
        if len(routes) < 2 or len(routes) != 2 + 4 * int.from_bytes(routes[:2], "little"):
            raise ProtocolError("Invalid WMS route list")

    def subscriber_number(self):
        self._allocate(_DMS)
        number = self.request(_DMS, 0x0024).get(1, b"").decode("ascii")
        if re.fullmatch(r"\+?[1-9][0-9]{1,14}", number):
            return "+" + number.lstrip("+")
        return ""

    def close(self):
        for service, cid in self.clients.items():
            with contextlib.suppress(Exception):
                self.request(_CTL, 0x0023, _tlv(1, bytes([service, cid])))

    def send(self, pdu, ims):
        data = _tlv(1, struct.pack("<BH", 6, len(pdu)) + pdu) + _tlv(0x13, bytes([ims]))
        try:
            fields = self.request(_WMS, _RAW_SEND, data)
            # Some firmware reports success without an optional message ID.
            reference = fields.get(1, b"")
            if len(reference) == 2:
                return str(int.from_bytes(reference, "little"))
            if not reference:
                return ""
            raise ProtocolError("Invalid WMS message ID")
        except Rejected as exc:
            # Report only explicit format, service, or delivery rejections as failures.
            if exc.code in {45, 48, 52, 53, 55, 56, 58, 69, 70, 71, 72, 73}:
                raise OutboundError(f"The modem rejected the message (QMI {exc.code}).") from None
            raise OutboundUnknown("The modem could not confirm submission.") from None
        except Exception:
            raise OutboundUnknown("The modem did not confirm submission.") from None


@contextlib.contextmanager
def open_wms(ssh, timeout, device):
    # Upload the helper inside a private directory. The shell removes it on exit.
    binary = _BRIDGE.read_bytes()
    if not 0 < len(binary) <= 8 * 1024 * 1024:
        raise ProtocolError("Invalid QMI bridge")
    script = f"""umask 077
case "$(uname -m)" in mips|mips64) ;; *) exit 1 ;; esac
usb_parent() {{
    path=$(readlink -f "$1/device") || return 1
    while [ -n "$path" ]; do
        if [ -r "$path/idVendor" ]; then printf '%s' "$path"; return 0; fi
        path=${{path%/*}}
    done
    return 1
}}
serial=$(readlink -f {shlex.quote(device)}) || exit 1
tty_usb=$(usb_parent "/sys/class/tty/${{serial##*/}}") || exit 1
qmi_usb=$(usb_parent "/sys/class/usbmisc/cdc-wdm0") || exit 1
[ "$tty_usb" = "$qmi_usb" ] || exit 1
tmp=$(mktemp -d /tmp/unifi-sms-qmi.XXXXXX) || exit 1
trap 'rm -f "$tmp/bridge"; rmdir "$tmp"' EXIT
trap 'exit 1' HUP INT TERM
head -c {len(binary)} > "$tmp/bridge" || exit 1
[ "$(wc -c < "$tmp/bridge")" -eq {len(binary)} ] || exit 1
chmod 700 "$tmp/bridge" || exit 1
"$tmp/bridge"
"""
    channel = None
    wms = None
    try:
        with command_deadline(ssh, timeout) as deadline:
            channel = ssh.get_transport().open_session(timeout=max(0.001, deadline - time.monotonic()))
            channel.settimeout(max(0.001, deadline - time.monotonic()))
            channel.exec_command(script)
            channel.sendall(binary)
        wms = WMS(channel, timeout)
        wms.open()
        yield wms
    finally:
        if wms is not None:
            with contextlib.suppress(Exception):
                wms.close()
        if channel is not None:
            with contextlib.suppress(Exception):
                channel.shutdown_write()
            with contextlib.suppress(Exception):
                channel.close()
