"""Decode SMS-DELIVER PDUs used by the modem archive."""

from dataclasses import dataclass
import re
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class DecodedSMS:
    sender: str
    text: str
    received_at: datetime
    concat_ref: str | None = None
    part: int = 1
    total: int = 1


_GSM = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_EXT = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}


def _take(data: bytes, pos: int, count: int) -> tuple[bytes, int]:
    if count < 0 or pos + count > len(data):
        raise ValueError("Truncated SMS PDU")
    return data[pos : pos + count], pos + count


def _septets(data: bytes, count: int, bit_offset: int = 0) -> str:
    if count < 0 or bit_offset + count * 7 > len(data) * 8:
        raise ValueError("Truncated GSM-7 data")
    chars = []
    escaped = False
    for index in range(count):
        bit = bit_offset + index * 7
        octet = bit // 8
        value = (data[octet] >> (bit % 8)) & 0x7F
        if bit % 8 > 1:
            value |= (data[octet + 1] << (8 - bit % 8)) & 0x7F
        if escaped:
            if value not in _EXT:
                raise ValueError("Unsupported GSM-7 extension")
            chars.append(_EXT[value])
            escaped = False
        elif value == 0x1B:
            escaped = True
        else:
            chars.append(_GSM[value])
    if escaped:
        raise ValueError("Incomplete GSM-7 extension")
    return "".join(chars)


def _bcd(value: int) -> int:
    tens, ones = value & 0x0F, value >> 4
    if tens > 9 or ones > 9:
        raise ValueError("Invalid BCD digit")
    return tens * 10 + ones


def _sender(raw: bytes, digits: int, toa: int) -> str:
    if toa & 0x70 == 0x50:
        return _septets(raw, digits * 4 // 7)
    if toa & 0x70 not in (0x00, 0x10, 0x20, 0x30):
        raise ValueError("Unsupported sender address")
    result = []
    for i in range(digits):
        nibble = (raw[i // 2] >> (4 * (i % 2))) & 0x0F
        if nibble > 9:
            raise ValueError("Unsupported sender digit")
        result.append(str(nibble))
    prefix = "+" if toa & 0x70 == 0x10 else ""
    return prefix + "".join(result)


def _timestamp(raw: bytes) -> datetime:
    year, month, day, hour, minute, second = map(_bcd, raw[:6])
    zone = raw[6]
    tens, ones = zone & 0x07, zone >> 4
    if tens > 9 or ones > 9:
        raise ValueError("Invalid timezone")
    quarters = tens * 10 + ones
    if quarters > 56:
        raise ValueError("Invalid timezone")
    offset = timedelta(minutes=15 * quarters)
    if zone & 0x08:
        offset = -offset
    return datetime(2000 + year, month, day, hour, minute, second, tzinfo=timezone(offset))


def _udh(raw: bytes) -> tuple[str | None, int, int, int]:
    if not raw:
        raise ValueError("Missing user data header")
    end = raw[0] + 1
    if end > len(raw):
        raise ValueError("Truncated user data header")
    ref, part, total = None, 1, 1
    pos = 1
    while pos < end:
        if pos + 2 > end:
            raise ValueError("Truncated information element")
        kind, length = raw[pos], raw[pos + 1]
        pos += 2
        if pos + length > end:
            raise ValueError("Truncated information element")
        value = raw[pos : pos + length]
        pos += length
        if kind in (0x00, 0x08):
            if length != (3 if kind == 0x00 else 4) or ref is not None:
                raise ValueError("Invalid concatenation header")
            ref = value[:-2].hex().upper()
            total, part = value[-2:]
            if not 1 <= part <= total:
                raise ValueError("Invalid multipart sequence")
        else:
            raise ValueError("Unsupported user data header element")
    return ref, part, total, end


def decode_pdu(raw: str) -> DecodedSMS:
    """Decode a complete SMS-DELIVER PDU or raise ValueError without data loss."""
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) % 2
        or re.fullmatch(r"[0-9A-Fa-f]+", raw) is None
    ):
        raise ValueError("Invalid PDU hex")
    try:
        data = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("Invalid PDU hex") from exc
    pos = 0
    service_length = data[pos]
    _, pos = _take(data, pos + 1, service_length)
    header, pos = _take(data, pos, 1)
    first = header[0]
    if first & 0x03:
        raise ValueError("Only SMS-DELIVER PDUs are supported")
    udhi = bool(first & 0x40)
    address_length, pos = _take(data, pos, 1)
    toa_raw, pos = _take(data, pos, 1)
    address_octets = (address_length[0] + 1) // 2
    address, pos = _take(data, pos, address_octets)
    sender = _sender(address, address_length[0], toa_raw[0])
    _, pos = _take(data, pos, 1)  # Protocol identifier.
    dcs_raw, pos = _take(data, pos, 1)
    dcs = dcs_raw[0]
    if dcs & 0xC0 == 0:
        if dcs & 0x20:
            raise ValueError("Compressed SMS is unsupported")
        coding = dcs & 0x0C
    elif dcs & 0xFC == 0xF0:
        coding = 0
    else:
        raise ValueError("Unsupported data coding scheme")
    if coding not in (0x00, 0x08):
        raise ValueError("Unsupported SMS encoding")
    stamp, pos = _take(data, pos, 7)
    received_at = _timestamp(stamp)
    udl_raw, pos = _take(data, pos, 1)
    udl = udl_raw[0]
    octets = (udl * 7 + 7) // 8 if coding == 0 else udl
    if octets > 140:
        raise ValueError("SMS user data exceeds 140 octets")
    payload, pos = _take(data, pos, octets)
    if pos != len(data):
        raise ValueError("Unexpected trailing PDU data")
    ref, part, total, header_octets = _udh(payload) if udhi else (None, 1, 1, 0)
    if coding == 0:
        header_septets = (header_octets * 8 + 6) // 7
        if header_septets > udl:
            raise ValueError("User data header exceeds message length")
        text = _septets(payload, udl - header_septets, header_septets * 7)
    else:
        body = payload[header_octets:]
        if len(body) % 2:
            raise ValueError("Odd UCS-2 body length")
        try:
            text = body.decode("utf-16-be")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid UCS-2 body") from exc
    return DecodedSMS(sender, text, received_at, ref, part, total)
