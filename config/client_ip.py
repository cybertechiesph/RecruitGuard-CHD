import ipaddress

from django.conf import settings


# Where our own proxies live: RFC 1918 private networks, IPv6 unique-local, loopback and
# link-local. (Python's is_private is broader, e.g. it includes documentation ranges.)
_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def _is_internal_hop(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_link_local
        or any(address in network for network in _INTERNAL_NETWORKS if network.version == address.version)
    )


def get_client_ip(request):
    """The visitor's IP address, for rate limits, throttles, captcha checks and audit logs.

    Behind our nginx proxy (RATE_LIMIT_TRUST_X_FORWARDED_FOR=True) the real address is the one
    nginx appends to X-Forwarded-For. Entries to its left are whatever the visitor sent and
    can be forged, so the header is read from the RIGHT, skipping private/loopback hops (our
    own proxies). Without a trusted proxy the header is ignored entirely: REMOTE_ADDR is the
    only address the visitor cannot choose."""
    if request is None:
        return ""
    meta = getattr(request, "META", {}) or {}
    remote_addr = meta.get("REMOTE_ADDR", "") or ""
    if not getattr(settings, "RATE_LIMIT_TRUST_X_FORWARDED_FOR", False):
        return remote_addr
    hops = [hop.strip() for hop in (meta.get("HTTP_X_FORWARDED_FOR", "") or "").split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_internal_hop(hop):
            return hop
    return hops[0] if hops else remote_addr
