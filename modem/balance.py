"""Validate and decode one-step carrier balance requests."""

import csv
import re

from .outbound import OutboundError, _address, _user_data


def balance_pdu(destination, text, smsc=None):
    """Encode a carrier SMS, including short destinations and an empty body."""
    if not isinstance(destination, str) or not re.fullmatch(r"\+?[0-9]{1,15}", destination):
        raise ValueError("Invalid carrier SMS destination.")
    if not isinstance(text, str):
        raise ValueError("Invalid carrier SMS text.")
    dcs, length, body = (0, 0, b"") if text == "" else _user_data(text)
    service = b""
    if smsc is not None:
        digits, toa = smsc
        if not re.fullmatch(r"[0-9]{1,20}", digits) or toa not in (129, 145, 161):
            raise ValueError("Invalid SMS center address.")
        service = bytes([toa]) + _address(digits)
    international = destination.startswith("+")
    digits = destination.lstrip("+")
    address = bytes([len(digits), 0x91 if international else 0x81]) + _address(digits)
    tpdu = b"\x01\x00" + address + bytes([0, dcs, length]) + body
    return bytes([len(service)]) + service + tpdu


def ussd_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[*#][0-9*#]{0,30}#", value):
        raise ValueError("Enter a literal one-step USSD code.")
    return value


def parse_cusd(line):
    """Return completed USSD text; reject menus and unsupported encodings."""
    try:
        fields = next(csv.reader([line.split(":", 1)[1]], strict=True, skipinitialspace=True))
        if len(fields) not in (1, 3) or fields[0] not in ("0", "1", "2", "3", "4", "5"):
            raise ValueError
        if fields[0] == "1":
            raise OutboundError("The carrier requires an interactive USSD session.")
        if fields[0] in ("3", "4", "5"):
            raise OutboundError("The carrier could not complete the USSD request.")
        if len(fields) == 1:
            return None
        text, dcs = fields[1], int(fields[2])
        if len(text) > 4096:
            raise ValueError
        if dcs in (8, 72):
            if re.fullmatch(r"(?:[0-9A-Fa-f]{4})*", text) is None:
                raise ValueError
            text = bytes.fromhex(text).decode("utf-16-be")
        elif dcs not in (0, 15):
            raise ValueError
        if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            raise ValueError
        return text or None
    except (ValueError, IndexError, csv.Error, UnicodeError):
        raise OutboundError("Invalid or unsupported USSD response.") from None
