import unittest
from datetime import timedelta

from modem.pdu import decode_pdu


def septets(values, bit_offset=0):
    packed = bytearray((bit_offset + len(values) * 7 + 7) // 8)
    for index, value in enumerate(values):
        bit = bit_offset + index * 7
        packed[bit // 8] |= (value << (bit % 8)) & 255
        if bit % 8 > 1:
            packed[bit // 8 + 1] |= value >> (8 - bit % 8)
    return bytes(packed)


def make_pdu(
    body,
    *,
    dcs=0,
    udl=None,
    first=0,
    sender="12345",
    toa=0x91,
    stamp=bytes.fromhex("62014021436580"),
):
    if toa == 0xD0:
        address = septets([ord(char) for char in sender])
        length = (len(sender) * 7 + 3) // 4
    else:
        length = len(sender)
        address = bytes(
            int((sender[i + 1] if i + 1 < length else "F") + sender[i], 16)
            for i in range(0, length, 2)
        )
    if udl is None:
        udl = len(body) if dcs == 8 else len(body) * 8 // 7
    return (
        (bytes([0, first, length, toa]) + address + bytes([0, dcs]) + stamp + bytes([udl]) + body)
        .hex()
        .upper()
    )


class DecodePduTests(unittest.TestCase):
    def test_gsm7_extension_and_positive_timezone(self):
        raw = make_pdu(septets([ord("H"), ord("i"), 27, 0x14]), udl=4)
        decoded = decode_pdu(raw)
        self.assertEqual((decoded.sender, decoded.text), ("+12345", "Hi^"))
        self.assertEqual(decoded.received_at.utcoffset(), timedelta(hours=2))
        self.assertEqual((decoded.part, decoded.total, decoded.concat_ref), (1, 1, None))

    def test_gsm7_class_coding(self):
        raw = make_pdu(septets([ord("A")]), dcs=0xF0, udl=1)
        self.assertEqual(decode_pdu(raw).text, "A")

    def test_ucs2_negative_timezone(self):
        raw = make_pdu("Привет".encode("utf-16-be"), dcs=8, stamp=bytes.fromhex("62014021436588"))
        decoded = decode_pdu(raw)
        self.assertEqual(decoded.text, "Привет")
        self.assertEqual(decoded.received_at.utcoffset(), -timedelta(hours=2))

    def test_multipart_eight_bit_gsm7(self):
        header = bytes.fromhex("050003AB0201")
        body = bytearray(8)
        body[:6] = header
        text = septets([ord("A"), ord("B")], bit_offset=49)
        body[6:] = text[6:]
        raw = make_pdu(bytes(body), first=0x40, udl=9)
        decoded = decode_pdu(raw)
        self.assertEqual(
            (decoded.text, decoded.concat_ref, decoded.part, decoded.total), ("AB", "AB", 1, 2)
        )

    def test_multipart_sixteen_bit_ucs2_and_alphanumeric_sender(self):
        body = bytes.fromhex("06080412340202") + "Z".encode("utf-16-be")
        raw = make_pdu(body, dcs=8, first=0x40, sender="INFO", toa=0xD0)
        decoded = decode_pdu(raw)
        self.assertEqual((decoded.sender, decoded.text), ("INFO", "Z"))
        self.assertEqual((decoded.concat_ref, decoded.part, decoded.total), ("1234", 2, 2))

    def test_alphanumeric_address_uses_semi_octet_length(self):
        # Four GSM septets occupy seven useful semi-octets.
        raw = "000007D049A7F1090000620140214365800141"
        self.assertEqual(decode_pdu(raw).sender, "INFO")

    def test_unsupported_language_table_is_retained(self):
        raw = make_pdu(bytes.fromhex("0325010100"), first=0x40, udl=5)
        with self.assertRaises(ValueError):
            decode_pdu(raw)

    def test_rejects_malformed_and_unsupported_pdus(self):
        good = make_pdu(septets([ord("A")]), udl=1)
        for raw in (
            good[:-2],
            good + "00",
            good[:-2] + "ZZ",
            make_pdu(b"\xff", dcs=4),
            make_pdu(b"\x1b", udl=1),
            make_pdu(bytes.fromhex("050003AB0200"), first=0x40, udl=7),
            make_pdu(b"\x00", dcs=8),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    decode_pdu(raw)


if __name__ == "__main__":
    unittest.main()
