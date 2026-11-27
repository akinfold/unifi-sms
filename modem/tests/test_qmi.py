import struct
import unittest

from modem.outbound import OutboundError, OutboundUnknown
from modem.qmi import WMS


class Proxy:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.pending = bytearray()
        self.sent = []

    def sendall(self, frame):
        self.sent.append(frame.hex())
        self.pending.extend(bytes.fromhex(next(self.replies)))

    def settimeout(self, timeout):
        pass

    def recv(self, size):
        count = min(size, 3)
        chunk = self.pending[:count]
        del self.pending[:count]
        return chunk


def response(service, cid, tx, msg, fields, *, flags=None):
    if flags is None:
        flags = 2 if service else 1
    fields = bytes.fromhex(fields)
    layout = "<BHHH" if service else "<BBHH"
    body = struct.pack(layout, flags, tx, msg, len(fields)) + fields
    return (struct.pack("<BHBBB", 1, len(body) + 5, 128, service, cid) + body).hex()


SUCCESS = "02040000000000"
OPEN = [
    response(0, 0, 1, 0xFF00, SUCCESS),
    response(0, 0, 2, 0x22, SUCCESS + "0102000507"),
    response(5, 7, 3, 0x33, SUCCESS + "010600010000000101"),
]


class QMITests(unittest.TestCase):
    def test_protocol_frames_allocation_registration_submission_and_release(self):
        channel = Proxy(OPEN + [
            response(5, 7, 0, 1, "", flags=4)
            + response(5, 7, 4, 0x20, SUCCESS + "0102003412"),
            response(0, 0, 5, 0x23, SUCCESS),
        ])
        wms = WMS(channel, 1)
        wms.open()
        self.assertEqual(wms.send(bytes.fromhex("0001"), False), "4660")
        wms.close()
        self.assertEqual(channel.sent, [
            "011b00000000000100ff1000010d002f6465762f6364632d77646d30",
            "010f0000000000022200040001010005",
            "010c0000050700030033000000",
            "01180000050700040020000c00010500060200000113010000",
            "0110000000000005230005000102000507",
        ])

    def test_dms_get_msisdn(self):
        channel = Proxy(OPEN + [
            response(0, 0, 4, 0x22, SUCCESS + "0102000206"),
            response(2, 6, 5, 0x24, SUCCESS + "010b00" + b"15550102003".hex()),
        ])
        wms = WMS(channel, 1)
        wms.open()
        self.assertEqual(wms.subscriber_number(), "+15550102003")

    def test_result_classification_and_explicit_ims_true(self):
        for reply, error in [
            (response(5, 7, 4, 0x20, "02040001003800"), OutboundError),
            (response(5, 7, 4, 0x20, "02040001002f00"), OutboundUnknown),
            (response(5, 7, 4, 0x20, "02040000000000" + "01010005"), OutboundUnknown),
            (response(5, 7, 4, 0x20, "02010000"), OutboundUnknown),
            ("", OutboundUnknown),
        ]:
            with self.subTest(reply=reply):
                channel = Proxy(OPEN + [reply])
                wms = WMS(channel, 1)
                wms.open()
                with self.assertRaises(error):
                    wms.send(b"\x01", True)
                self.assertEqual(len(channel.sent), 4)
                self.assertTrue(channel.sent[-1].endswith("13010001"))

    def test_missing_optional_message_reference_is_accepted(self):
        channel = Proxy(OPEN + [response(5, 7, 4, 0x20, SUCCESS)])
        wms = WMS(channel, 1)
        wms.open()
        self.assertEqual(wms.send(b"\x01", True), "")
