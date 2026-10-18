"""Find UniFi LTE devices and reachable SSH candidates on private networks."""

import errno
import ipaddress
import re
import select
import selectors
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from pathlib import Path

import dns.exception
import dns.resolver

try:
    import fcntl
except ImportError:  # pragma: no cover - the application container runs Linux
    fcntl = None


DISCOVERY_PORT = 10001
SCAN_SECONDS = 4.5
# Docker Desktop may take more than one TCP retransmission to reach a LAN host.
SEARCH_SECONDS = 60
SSH_CONNECT_SECONDS = 2
SSH_BANNER_SECONDS = 1.5
MAX_SSH_SOCKETS = 64
DNS_SECONDS = 2
DNS_QUERY_SECONDS = 1
MAX_DNS_WORKERS = 8
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_COMMON_NETWORKS = ("192.168.0.0/24", "192.168.1.0/24", "10.0.0.0/24", "10.0.1.0/24")
MAX_NETWORK_ADDRESSES = 256
_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_PROBES = (b"\x01\x00\x00\x00", b"\x02\x08\x00\x00")


def _private_address(address):
    return any(address in network for network in _PRIVATE_NETWORKS)


def _network(value):
    try:
        network = ipaddress.ip_network(value, strict=False)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid private IPv4 subnet.") from None
    if (
        not isinstance(network, ipaddress.IPv4Network)
        or network.num_addresses > MAX_NETWORK_ADDRESSES
        or not any(network.subnet_of(private) for private in _PRIVATE_NETWORKS)
    ):
        raise ValueError("Choose an RFC 1918 IPv4 subnet of at most 256 addresses.")
    return network


def discover_networks():
    """List attached private IPv4 subnets, capped at 256 addresses each."""
    if fcntl is None:
        return []
    found = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, interface in socket.if_nameindex():
            encoded = interface.encode("ascii", errors="ignore")[:15]
            if not encoded:
                continue
            request = struct.pack("256s", encoded)
            try:
                address = ipaddress.IPv4Address(
                    fcntl.ioctl(sock.fileno(), 0x8915, request)[20:24]
                )
                mask = ipaddress.IPv4Address(
                    fcntl.ioctl(sock.fileno(), 0x891B, request)[20:24]
                )
                connected = ipaddress.IPv4Network((address, str(mask)), strict=False)
            except (OSError, ValueError):
                continue
            if not _private_address(address):
                continue
            if connected.num_addresses > MAX_NETWORK_ADDRESSES:
                connected = ipaddress.IPv4Network((address, 24), strict=False)
            if any(connected.subnet_of(private) for private in _PRIVATE_NETWORKS):
                found.add(connected)
    return [str(item) for item in sorted(found, key=lambda item: int(item.network_address))]


def _lte_model(value):
    try:
        model = value.decode("ascii").strip(" \x00")
    except UnicodeDecodeError:
        return None
    normalized = "".join(character for character in model.upper() if character.isalnum())
    if normalized in {
        "ULTE", "ULTEUS", "ULTEPRO", "ULTEPROEU", "ULTEPROUS",
        "ULTEBACKUPPRO", "ULTEBACKUPPROEU", "ULTEBACKUPPROUS",
    }:
        return model
    return None


def _parse_response(data, source, network=None):
    try:
        address = ipaddress.IPv4Address(source)
    except ipaddress.AddressValueError:
        return None
    if not _private_address(address) or network is not None and address not in network:
        return None
    if len(data) < 4 or (data[0], data[1]) not in {
        (1, 0), (2, 6), (2, 9), (2, 11)
    } or int.from_bytes(data[2:4], "big") != len(data) - 4:
        return None

    model = None
    name = ""
    mac = None
    advertised = []
    offset = 4
    while offset < len(data):
        if offset + 3 > len(data):
            return None
        kind = data[offset]
        length = int.from_bytes(data[offset + 1:offset + 3], "big")
        offset += 3
        if offset + length > len(data):
            return None
        value = data[offset:offset + length]
        offset += length
        if kind == 0x01 and length == 6:
            mac = ":".join(f"{byte:02x}" for byte in value)
        elif kind == 0x02 and length == 10:
            advertised.append(
                (ipaddress.IPv4Address(value[6:10]), ":".join(f"{byte:02x}" for byte in value[:6]))
            )
        elif kind in (0x0C, 0x14, 0x15):
            candidate_model = _lte_model(value)
            if candidate_model:
                model = candidate_model
        elif kind == 0x0B:
            name = "".join(
                character for character in value.decode("utf-8", errors="replace")
                if character.isprintable()
            )[:80]

    if advertised:
        mac = next((item_mac for item_ip, item_mac in advertised if item_ip == address), None)
    if not model or not mac or mac == "00:00:00:00:00:00":
        return None
    return {"address": str(address), "model": model, "name": name, "mac": mac}


def search_networks(network=None):
    """Choose bounded private subnets for one search.

    Docker bridge interfaces do not reveal the host LAN. In a container,
    also try four common LAN subnets. An explicit subnet replaces these hints.
    """
    if network is not None:
        return [str(_network(network))]
    networks = discover_networks()[:3]
    if Path("/.dockerenv").exists():
        networks = list(dict.fromkeys([*networks, *_COMMON_NETWORKS]))
    return networks


def _udp_devices(networks, selected=None):
    broadcasts = {"255.255.255.255"} if selected is None else set()
    hosts = set()
    for candidate in networks:
        if candidate.num_addresses > 2:
            broadcasts.add(str(candidate.broadcast_address))
            hosts.update(str(host) for host in candidate.hosts())
        else:
            hosts.update(str(host) for host in candidate)

    found = {}
    deadline = time.monotonic() + SCAN_SECONDS
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        targets = sorted(broadcasts) + sorted(hosts, key=lambda item: int(ipaddress.IPv4Address(item)))
        for target in targets:
            for probe in _PROBES:
                if time.monotonic() >= deadline:
                    break
                try:
                    sock.sendto(probe, (target, DISCOVERY_PORT))
                except OSError:
                    continue

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([sock], [], [], remaining)
            if not readable:
                break
            try:
                payload, sender = sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                continue
            if sender[1] != DISCOVERY_PORT:
                continue
            device = _parse_response(payload, sender[0], selected)
            if device:
                found[(device["address"], device["mac"])] = device
    return sorted(found.values(), key=lambda item: ipaddress.IPv4Address(item["address"]))


def _candidate_addresses(networks):
    """Interleave subnets so one slow subnet cannot consume the search budget."""
    seen = set()
    for group in zip_longest(*(network.hosts() for network in networks)):
        for address in group:
            if address is not None and address not in seen:
                seen.add(address)
                yield str(address)


def _ssh_candidates(networks, port=22):
    """Read SSH banners without authenticating or executing device commands."""
    addresses = _candidate_addresses(networks)
    pending = {}
    found = []
    exhausted = False
    deadline = time.monotonic() + SEARCH_SECONDS
    with selectors.DefaultSelector() as selector:
        def close(sock):
            selector.unregister(sock)
            pending.pop(sock)
            sock.close()

        try:
            while time.monotonic() < deadline:
                while not exhausted and len(pending) < MAX_SSH_SOCKETS:
                    address = next(addresses, None)
                    if address is None:
                        exhausted = True
                        break
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        sock.setblocking(False)
                        error = sock.connect_ex((address, port))
                        if error not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK,
                                         errno.EALREADY, errno.EINTR):
                            sock.close()
                            continue
                        selector.register(sock, selectors.EVENT_WRITE)
                        pending[sock] = {
                            "address": address, "connected": False, "data": b"",
                            "expires": time.monotonic() + SSH_CONNECT_SECONDS,
                        }
                    except OSError:
                        sock.close()
                if not pending and exhausted:
                    break
                now = time.monotonic()
                for sock, state in list(pending.items()):
                    if now >= state["expires"]:
                        close(sock)
                if not pending:
                    continue
                timeout = max(0, min(deadline - now,
                                     min(state["expires"] for state in pending.values()) - now))
                for key, _ in selector.select(timeout):
                    sock = key.fileobj
                    state = pending[sock]
                    try:
                        if not state["connected"]:
                            if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                                close(sock)
                                continue
                            state["connected"] = True
                            state["expires"] = time.monotonic() + SSH_BANNER_SECONDS
                            selector.modify(sock, selectors.EVENT_READ)
                            continue
                        chunk = sock.recv(256 - len(state["data"]))
                        state["data"] += chunk
                        if state["data"].startswith((b"SSH-2.0-", b"SSH-1.99-")):
                            found.append({
                                "address": state["address"], "model": "", "name": "",
                                "mac": "", "source": "ssh", "confirmed": False,
                            })
                            close(sock)
                        elif not chunk or b"\n" in state["data"] or len(state["data"]) >= 255:
                            close(sock)
                    except BlockingIOError:
                        continue
                    except OSError:
                        close(sock)
        finally:
            for sock in list(pending):
                close(sock)
    return found



def _reverse_hostname(resolver, address, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    try:
        answers = resolver.resolve_address(address, lifetime=min(DNS_QUERY_SECONDS, remaining))
        hostname = answers[0].target.to_text(omit_final_dot=True)
    except (dns.exception.DNSException, OSError, IndexError):
        return ""
    if len(hostname) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in hostname.split(".")):
        return ""
    return hostname


def _with_hostnames(devices):
    """Add bounded PTR hints without treating them as model or identity checks."""
    if not devices:
        return []
    hostnames = [""] * len(devices)
    try:
        resolver = dns.resolver.Resolver()
    except (dns.exception.DNSException, OSError):
        resolver = None
    if resolver is not None:
        deadline = time.monotonic() + DNS_SECONDS
        with ThreadPoolExecutor(max_workers=MAX_DNS_WORKERS) as pool:
            hostnames = list(pool.map(
                lambda device: _reverse_hostname(resolver, device["address"], deadline), devices,
            ))
    results = []
    for device, hostname in zip(devices, hostnames):
        label = hostname.partition(".")[0]
        inferred_model = _lte_model(label.encode("ascii"))
        likely_lte = bool(inferred_model and "PRO" in inferred_model.upper())
        results.append({**device, "hostname": hostname, "likely_lte": likely_lte})
    return results


def discover_devices(network=None):
    """Return UDP-identified LTE devices and unverified SSH candidates.

    Search at most seven /24-sized private networks for up to sixty seconds,
    then spend up to two seconds on PTR hints. SSH candidates still require
    the wizard's authenticated model check.
    """
    networks = [_network(value) for value in search_networks(network)]
    selected = _network(network) if network is not None else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        udp = pool.submit(_udp_devices, networks, selected)
        ssh = pool.submit(_ssh_candidates, networks)
        candidates = ssh.result()
        try:
            devices = udp.result()
        except OSError:
            devices = []
    found = {device["address"]: device for device in candidates}
    for device in devices:
        found[device["address"]] = {**device, "source": "unifi", "confirmed": True}
    return sorted(_with_hostnames(list(found.values())), key=lambda item: (
        0 if item.get("confirmed") else 1 if item["likely_lte"] else 2,
        ipaddress.IPv4Address(item["address"]),
    ))
