from ipaddress import ip_address, ip_network

from django.conf import settings


def _address(value):
    try:
        return ip_address(value)
    except ValueError:
        return None


def trusted_peer(value):
    address = _address(value)
    if address is None:
        return False
    return any(address in ip_network(cidr) for cidr in settings.TRUSTED_PROXY_CIDRS)


def client_address(request):
    peer = request.META.get("REMOTE_ADDR", "")
    address = _address(peer)
    if address is None:
        return "unknown"
    if not trusted_peer(peer):
        return str(address)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if not forwarded or len(forwarded) > 2048:
        return str(address)
    chain = forwarded.split(",")
    if len(chain) > 32:
        return str(address)
    # Only the trusted suffix can establish the next client address.
    for hop in reversed(chain):
        candidate = _address(hop.strip())
        if candidate is None:
            return str(address)
        address = candidate
        if not trusted_peer(str(address)):
            break
    return str(address)
