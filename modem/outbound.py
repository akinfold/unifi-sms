"""Encode single-segment SMS-SUBMIT messages."""

import re

from .pdu import _EXT, _GSM


class OutboundError(RuntimeError):
    """The modem rejected the message, or submission never started."""


class OutboundSimChanged(OutboundError):
    """The current SIM differs from the SIM selected for submission."""


class OutboundUnknown(RuntimeError):
    """Submission started, but the modem's result is uncertain."""


_SEPTETS = {char: bytes([index]) for index, char in enumerate(_GSM) if index != 0x1B}
_SEPTETS.update({char: bytes([0x1B, index]) for index, char in _EXT.items()})


def _user_data(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Enter a message.")
    if len(text) > 160:
        raise ValueError("The message exceeds one SMS segment.")
    try:
        septets = b"".join(_SEPTETS[char] for char in text)
    except KeyError:
        try:
            data = text.encode("utf-16-be")
        except UnicodeEncodeError:
            raise ValueError("The message contains invalid Unicode.") from None
        if len(data) > 140:
            raise ValueError("The message exceeds one SMS segment.")
        return 8, len(data), data
    if len(septets) > 160:
        raise ValueError("The message exceeds one SMS segment.")
    packed = sum(value << (7 * index) for index, value in enumerate(septets))
    return 0, len(septets), packed.to_bytes((7 * len(septets) + 7) // 8, "little")


def validate_text(text):
    """Raise ValueError if the text cannot fit in one SMS."""
    _user_data(text)


def _address(digits):
    if len(digits) % 2:
        digits += "F"
    return bytes.fromhex("".join(digits[i : i + 2][::-1] for i in range(0, len(digits), 2)))


def submit_pdu(phone, text, smsc=None):
    if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{1,14}", phone):
        raise ValueError("Enter a phone number in international format.")
    dcs, length, body = _user_data(text)
    service = b""
    if smsc is not None:
        digits, toa = smsc
        if not re.fullmatch(r"[0-9]{1,20}", digits) or toa not in (129, 145, 161):
            raise ValueError("Invalid SMS center address.")
        service = bytes([toa]) + _address(digits)
    destination = bytes([len(phone) - 1, 0x91]) + _address(phone[1:])
    tpdu = b"\x01\x00" + destination + bytes([0, dcs, length]) + body
    return bytes([len(service)]) + service + tpdu
