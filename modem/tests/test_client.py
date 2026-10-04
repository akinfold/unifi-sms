import unittest

from modem.client import ModemClient, ModemError


class FakeChannel:
    def __init__(self, response):
        self.chunks = [response[i : i + 3] for i in range(0, len(response), 3)]
        self.sent = []
        self.closed = False

    def sendall(self, value):
        self.sent.append(value)

    def settimeout(self, value):
        pass

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""


class ClientTests(unittest.TestCase):
    def client(self):
        return ModemClient("example.invalid", 22, "user", "key", "known_hosts")

    def test_fragmented_cmgl_with_echo_and_urc(self):
        client = self.client()
        client._channel = FakeChannel(
            b'__UNIFI_SMS_BEGIN__\nAT+CMGL=4\r\n+CMTI: "SM",8\r\n'
            b"+CMGL: 0,1,,23\r\n001122\r\n+CMGL: 2,1,,23\r\nAABBCC\r\nOK\r\n"
        )
        self.assertEqual(client.list_pdus(), {0: "001122", 2: "AABBCC"})
        self.assertEqual(client._channel.sent, ["+CMGL=4\n"])

    def test_cnum_reads_normalizes_and_deduplicates_numbers(self):
        client = self.client()
        client._channel = FakeChannel(
            b'__UNIFI_SMS_BEGIN__\nAT+CNUM\r\n+CMTI: "SM",2\r\n'
            b'+CNUM: "Voice, primary", "15550102001", 145,,4\r\n'
            b'+CNUM: "", "+15550102001",145\r\n'
            b'+CNUM: "Local","020123456",129\r\nOK\r\n'
        )
        self.assertEqual(client.phone_numbers(), ["+15550102001", "020123456"])
        self.assertEqual(client._channel.sent, ["+CNUM\n"])

    def test_cnum_empty_number_is_supported(self):
        for response in (b"", b'+CNUM: "","",129\r\n'):
            client = self.client()
            client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n" + response + b"OK\r\n")
            self.assertEqual(client.phone_numbers(), [])

    def test_cnum_rejects_invalid_responses(self):
        for response in (
            b'+CNUM: "missing number"',
            b'+CNUM: "","123",invalid',
            b'+CNUM: "","<script>",145',
            b'+CNUM: "","unterminated',
            b"unexpected",
            b"ERROR",
        ):
            with self.subTest(response=response):
                client = self.client()
                client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n" + response + b"\r\nOK\r\n")
                with self.assertRaises(ModemError):
                    client.phone_numbers()

    def test_missing_read_and_error(self):
        client = self.client()
        client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n+CMS ERROR: 321\r\n")
        self.assertIsNone(client.read_pdu(0))
        client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\nERROR\r\n")
        with self.assertRaises(ModemError):
            client.read_pdu(0)

    def test_malformed_listing_does_not_look_like_empty_storage(self):
        for response in (b"+CMGL: invalid", b"001122", b"unexpected"):
            client = self.client()
            client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n" + response + b"\r\nOK\r\n")
            with self.assertRaises(ModemError):
                client.list_pdus()

    def test_memory_full_is_not_a_missing_slot(self):
        client = self.client()
        client._channel = FakeChannel(b"__UNIFI_SMS_BEGIN__\n+CMS ERROR: 322\r\n")
        with self.assertRaises(ModemError):
            client.read_pdu(0)

    def test_rejects_invalid_slots_before_sending(self):
        client = self.client()
        client._channel = FakeChannel(b"")
        for slot in (-1, True, 1.0, "1"):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                client.delete(slot)
        self.assertEqual(client._channel.sent, [])


if __name__ == "__main__":
    unittest.main()
