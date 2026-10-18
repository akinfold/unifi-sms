import ipaddress
import socket
import struct
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import dns.exception
import dns.resolver

from django.test import SimpleTestCase

from . import discovery


def packet(model=b"U-LTE-Pro-EU", address="192.168.50.10", name=b"LTE backup"):
    mac = bytes.fromhex("021122334455")
    fields = (
        (0x02, mac + ipaddress.IPv4Address(address).packed),
        (0x0B, name),
        (0x15, model),
    )
    payload = b"".join(bytes([kind]) + struct.pack(">H", len(value)) + value for kind, value in fields)
    return b"\x02\x06" + struct.pack(">H", len(payload)) + payload


class DiscoveryParsingTests(SimpleTestCase):
    def test_lte_response_uses_sender_and_matching_advertised_address(self):
        device = discovery._parse_response(packet(), "192.168.50.10")
        self.assertEqual(device, {
            "address": "192.168.50.10",
            "model": "U-LTE-Pro-EU",
            "name": "LTE backup",
            "mac": "02:11:22:33:44:55",
        })

    def test_unrelated_or_unconfirmed_model_is_hidden(self):
        for model in (b"U6-Pro", b"ULTE-Evil", b""):
            with self.subTest(model=model):
                self.assertIsNone(discovery._parse_response(packet(model=model), "192.168.50.10"))

    def test_legacy_product_tag_survives_an_unrecognized_model_tag(self):
        model = b"U-LTE-Pro"
        body = (
            b"\x01\x00\x06" + bytes.fromhex("021122334455")
            + b"\x0c" + struct.pack(">H", len(model)) + model
            + b"\x14\x00\x03OEM"
        )
        response = b"\x01\x00" + struct.pack(">H", len(body)) + body
        self.assertEqual(discovery._parse_response(response, "10.1.2.3")["model"], "U-LTE-Pro")

    def test_malformed_or_untrusted_reply_is_hidden(self):
        valid = packet()
        self.assertIsNone(discovery._parse_response(valid[:-1], "192.168.50.10"))
        self.assertIsNone(discovery._parse_response(valid, "192.168.50.11"))
        self.assertIsNone(discovery._parse_response(valid, "8.8.8.8"))
        self.assertIsNone(
            discovery._parse_response(valid, "192.168.50.10", ipaddress.ip_network("10.0.0.0/24"))
        )
        self.assertIsNone(discovery._parse_response(b"\x02\x08" + valid[2:], "192.168.50.10"))

    def test_v1_model_tag_is_supported(self):
        model = b"U-LTE"
        body = (
            b"\x01\x00\x06" + bytes.fromhex("021122334455")
            + b"\x14" + struct.pack(">H", len(model)) + model
        )
        response = b"\x01\x00" + struct.pack(">H", len(body)) + body
        self.assertEqual(discovery._parse_response(response, "10.1.2.3")["model"], "U-LTE")


class DiscoveryScanTests(SimpleTestCase):
    def test_rejects_public_and_large_networks_before_socket_use(self):
        with patch.object(discovery.socket, "socket") as open_socket:
            for network in ("8.8.8.0/24", "192.168.0.0/16", "not-a-subnet", "::1/128"):
                with self.subTest(network=network), self.assertRaises(ValueError):
                    discovery.discover_devices(network)
            open_socket.assert_not_called()

    def test_selected_host_receives_both_probes_and_returns_only_lte(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.recvfrom.side_effect = [
            (packet(), ("192.168.50.10", discovery.DISCOVERY_PORT)),
            (packet(model=b"U6-Pro"), ("192.168.50.10", discovery.DISCOVERY_PORT)),
        ]
        with (
            patch.object(discovery.socket, "socket", return_value=sock),
            patch.object(discovery.select, "select", side_effect=[
                ([sock], [], []), ([sock], [], []), ([], [], [])
            ]) as wait,
        ):
            devices = discovery._udp_devices([ipaddress.ip_network("192.168.50.10/32")], ipaddress.ip_network("192.168.50.10/32"))
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["address"], "192.168.50.10")
        self.assertEqual(
            {call.args for call in sock.sendto.call_args_list},
            {(probe, ("192.168.50.10", discovery.DISCOVERY_PORT)) for probe in discovery._PROBES},
        )
        self.assertLessEqual(wait.call_args_list[0].args[3], discovery.SCAN_SECONDS)

    def test_no_attached_subnet_only_sends_limited_broadcast(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        with (
            patch.object(discovery, "discover_networks", return_value=[]),
            patch.object(discovery.socket, "socket", return_value=sock),
            patch.object(discovery.select, "select", return_value=([], [], [])),
        ):
            self.assertEqual(discovery._udp_devices([]), [])
        self.assertEqual(
            {call.args[1][0] for call in sock.sendto.call_args_list},
            {"255.255.255.255"},
        )


@contextmanager
def banner_server(banner, delay=0):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)
        received = []

        def serve():
            try:
                client, _ = server.accept()
                with client:
                    client.settimeout(2)
                    if delay:
                        time.sleep(delay)
                    if banner:
                        client.sendall(banner)
                    try:
                        received.append(client.recv(128))
                    except socket.timeout:
                        pass
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield server.getsockname()[1], received
        finally:
            thread.join(timeout=2)


class SSHDiscoveryTests(SimpleTestCase):
    def setUp(self):
        resolver_patch = patch.object(discovery.dns.resolver, "Resolver")
        resolver = resolver_patch.start()
        self.addCleanup(resolver_patch.stop)
        resolver.return_value.resolve_address.side_effect = dns.resolver.NXDOMAIN()

    def test_subnets_are_interleaved_and_overlapping_hosts_are_not_scanned_twice(self):
        networks = [ipaddress.ip_network(cidr) for cidr in (
            "10.20.30.0/30", "192.168.50.0/30", "10.20.30.1/32",
        )]
        self.assertEqual(list(discovery._candidate_addresses(networks)), [
            "10.20.30.1", "192.168.50.1", "10.20.30.2", "192.168.50.2",
        ])

    def test_banner_fallback_finds_candidate_without_sending_credentials(self):
        with banner_server(b"SSH-2.0-dropbear_2024.86\r\n") as (port, received):
            results = discovery._ssh_candidates([ipaddress.ip_network("127.0.0.1/32")], port)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["address"], "127.0.0.1")
        self.assertFalse(results[0]["confirmed"])
        self.assertEqual(results[0]["model"], "")
        self.assertEqual(received, [b""])

    def test_delayed_tcp_connection_is_not_discarded_before_it_can_respond(self):
        class DelayedConnectionSelector(discovery.selectors.DefaultSelector):
            def __init__(self):
                super().__init__()
                self.ready_after = time.monotonic() + 0.8

            def select(self, timeout=None):
                remaining = self.ready_after - time.monotonic()
                if remaining > 0:
                    time.sleep(min(remaining, timeout) if timeout is not None else remaining)
                    return []
                return super().select(timeout)

        with (banner_server(b"SSH-2.0-dropbear_2024.86\r\n") as (port, received),
              patch.object(discovery.selectors, "DefaultSelector", DelayedConnectionSelector)):
            results = discovery._ssh_candidates([ipaddress.ip_network("127.0.0.1/32")], port)
        self.assertEqual([device["address"] for device in results], ["127.0.0.1"])
        self.assertEqual(received, [b""])

    def test_delayed_ssh_banner_remains_an_unverified_candidate(self):
        with banner_server(b"SSH-2.0-dropbear_2024.86\r\n", delay=0.8) as (port, received):
            results = discovery._ssh_candidates([ipaddress.ip_network("127.0.0.1/32")], port)
        self.assertEqual([device["address"] for device in results], ["127.0.0.1"])
        self.assertFalse(results[0]["confirmed"])
        self.assertEqual(received, [b""])

    def test_non_ssh_service_is_not_shown_as_a_candidate(self):
        with banner_server(b"HTTP/1.1 200 OK\r\n") as (port, _):
            self.assertEqual(discovery._ssh_candidates(
                [ipaddress.ip_network("127.0.0.1/32")], port,
            ), [])

    def test_silent_service_does_not_extend_search_deadline(self):
        with banner_server(b"") as (port, _), patch.object(discovery, "SEARCH_SECONDS", 0.15):
            started = time.monotonic()
            self.assertEqual(discovery._ssh_candidates(
                [ipaddress.ip_network("127.0.0.1/32")], port,
            ), [])
            self.assertLess(time.monotonic() - started, 0.4)

    def test_container_search_includes_common_lans_and_explicit_subnet_replaces_them(self):
        with (
            patch.object(discovery, "discover_networks", return_value=["172.20.0.0/24"]),
            patch.object(discovery.Path, "exists", return_value=True),
        ):
            automatic = discovery.search_networks()
            self.assertIn("192.168.1.0/24", automatic)
            self.assertIn("172.20.0.0/24", automatic)
            self.assertEqual(discovery.search_networks("10.20.30.10/32"), ["10.20.30.10/32"])
        self.assertLessEqual(len(automatic), 7)

    def test_confirmed_udp_model_replaces_ssh_candidate_for_the_same_address(self):
        candidate = {"address": "192.168.50.10", "model": "", "confirmed": False}
        identified = discovery._parse_response(packet(), "192.168.50.10")
        with (
            patch.object(discovery, "search_networks", return_value=["192.168.50.0/24"]),
            patch.object(discovery, "_ssh_candidates", return_value=[candidate]),
            patch.object(discovery, "_udp_devices", return_value=[identified]),
        ):
            results = discovery.discover_devices()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["model"], "U-LTE-Pro-EU")
        self.assertTrue(results[0]["confirmed"])

    def test_udp_socket_failure_still_returns_reachable_ssh_candidate(self):
        candidate = {"address": "10.20.30.10", "model": "", "confirmed": False}
        with (
            patch.object(discovery, "search_networks", return_value=["10.20.30.0/24"]),
            patch.object(discovery, "_ssh_candidates", return_value=[candidate]),
            patch.object(discovery, "_udp_devices", side_effect=OSError("UDP unavailable")),
        ):
            self.assertEqual(discovery.discover_devices(), [
                {**candidate, "hostname": "", "likely_lte": False},
            ])


class DNSDiscoveryTests(SimpleTestCase):
    def setUp(self):
        resolver_patch = patch.object(discovery.dns.resolver, "Resolver")
        self.resolver = resolver_patch.start().return_value
        self.addCleanup(resolver_patch.stop)

    @staticmethod
    def answer(hostname):
        target = MagicMock()
        target.to_text.return_value = hostname
        return [MagicMock(target=target)]

    def test_dns_model_hint_preserves_unverified_connection_state(self):
        candidate = {
            "address": "10.20.30.40", "source": "ssh", "confirmed": False,
            "model": "", "name": "", "mac": "",
        }
        self.resolver.resolve_address.return_value = self.answer("U-LTE-Pro.localdomain")
        device = discovery._with_hostnames([candidate])[0]
        self.assertEqual(device["hostname"], "U-LTE-Pro.localdomain")
        self.assertTrue(device["likely_lte"])
        self.assertFalse(device["confirmed"])
        self.assertEqual(device["model"], "")
        self.assertEqual(device["source"], "ssh")
        self.assertNotIn("hostname", candidate)

    def test_only_recognized_lte_pro_host_labels_are_prioritized(self):
        for hostname, expected in (
            ("u-lte-pro.example.invalid", True),
            ("ULTEPROEU.example.invalid", True),
            ("u-lte-backup-pro-us.example.invalid", True),
            ("U-LTE.example.invalid", False),
            ("U7-Pro.example.invalid", False),
            ("U-LTE-Pro-other.example.invalid", False),
            ("gateway.U-LTE-Pro.example.invalid", False),
        ):
            with self.subTest(hostname=hostname):
                self.resolver.resolve_address.return_value = self.answer(hostname)
                device = discovery._with_hostnames([{"address": "10.20.30.40"}])[0]
                self.assertEqual(device["hostname"], hostname)
                self.assertEqual(device["likely_lte"], expected)

    def test_invalid_or_escaped_hostname_is_not_displayed(self):
        for hostname in ("", "u-lte-pro..invalid", "<script>.invalid", "lte\\032pro.invalid",
                         "U-LTE-Pro\n.invalid", "x" * 64 + ".invalid", "x." * 128 + "invalid"):
            with self.subTest(hostname=hostname):
                self.resolver.resolve_address.return_value = self.answer(hostname)
                device = discovery._with_hostnames([{"address": "10.20.30.40"}])[0]
                self.assertEqual(device["hostname"], "")
                self.assertFalse(device["likely_lte"])

    def test_dns_errors_leave_candidates_available(self):
        for error in (dns.resolver.NXDOMAIN(), dns.resolver.NoAnswer(),
                      dns.exception.Timeout(), dns.resolver.NoNameservers(), OSError("unavailable")):
            with self.subTest(error=type(error).__name__):
                self.resolver.resolve_address.side_effect = error
                self.assertEqual(discovery._with_hostnames([
                    {"address": "10.20.30.40", "confirmed": False},
                ]), [{"address": "10.20.30.40", "confirmed": False,
                      "hostname": "", "likely_lte": False}])

    def test_missing_resolver_does_not_remove_candidates(self):
        with patch.object(discovery.dns.resolver, "Resolver", side_effect=dns.resolver.NoResolverConfiguration()):
            results = discovery._with_hostnames([{"address": "10.20.30.40"}])
        self.assertEqual(results, [{"address": "10.20.30.40", "hostname": "", "likely_lte": False}])

    def test_dns_uses_remaining_budget_and_skips_expired_queries(self):
        self.resolver.resolve_address.return_value = self.answer("U-LTE-Pro.invalid")
        with patch.object(discovery.time, "monotonic", return_value=10):
            self.assertEqual(discovery._reverse_hostname(self.resolver, "10.20.30.40", 10), "")
            self.resolver.resolve_address.assert_not_called()
            discovery._reverse_hostname(self.resolver, "10.20.30.40", 10.25)
        self.resolver.resolve_address.assert_called_once_with("10.20.30.40", lifetime=0.25)

    def test_many_slow_queries_share_one_search_budget(self):
        def slow_lookup(address, lifetime):
            time.sleep(lifetime)
            raise dns.exception.Timeout()

        self.resolver.resolve_address.side_effect = slow_lookup
        candidates = [{"address": f"10.20.30.{number}"} for number in range(1, 21)]
        with (patch.object(discovery, "DNS_SECONDS", 0.08),
              patch.object(discovery, "DNS_QUERY_SECONDS", 0.05),
              patch.object(discovery, "MAX_DNS_WORKERS", 2)):
            started = time.monotonic()
            results = discovery._with_hostnames(candidates)
            self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(len(results), len(candidates))
        self.assertTrue(all(not device["hostname"] for device in results))
        self.assertLess(self.resolver.resolve_address.call_count, len(candidates))

    def test_confirmed_devices_then_dns_hints_then_other_candidates(self):
        candidates = [
            {"address": "10.20.30.10", "confirmed": False, "model": ""},
            {"address": "10.20.30.40", "confirmed": False, "model": ""},
        ]
        identified = {"address": "10.20.30.50", "model": "U-LTE-Pro-EU", "name": "LTE backup"}
        def lookup(address, lifetime):
            name = "U-LTE-Pro.invalid" if address.endswith(".40") else "switch.invalid"
            return self.answer(name)

        self.resolver.resolve_address.side_effect = lookup
        with (patch.object(discovery, "search_networks", return_value=["10.20.30.0/24"]),
              patch.object(discovery, "_ssh_candidates", return_value=candidates),
              patch.object(discovery, "_udp_devices", return_value=[identified])):
            results = discovery.discover_devices()
        self.assertEqual([device["address"] for device in results],
                         ["10.20.30.50", "10.20.30.40", "10.20.30.10"])
        self.assertTrue(results[0]["confirmed"])
        self.assertTrue(results[1]["likely_lte"])
        self.assertFalse(results[1]["confirmed"])
        self.assertEqual(results[1]["model"], "")
