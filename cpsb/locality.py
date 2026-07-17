"""Client-locality gate: is an HTTP request's peer this machine (PROTOCOL.md §7)?

Used by ``POST /cpsb/open`` (:mod:`cpsb.routes`) to decide whether a Tier 1
(OS-level) Photoshop launch would put a document on a screen the requesting
browser can actually see. Dependency-free by design (stdlib :mod:`socket`
only) -- no interface-enumeration library is needed because the bind-test
below answers the question directly.
"""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger("cpsb")

#: Peer addresses that are always this machine, without needing a bind test.
_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})

#: Presence of either means ``request.remote`` is a reverse proxy's own
#: address, not the real client's -- locality becomes unknowable from this
#: request alone.
_FORWARDING_HEADERS = ("X-Forwarded-For", "X-Real-IP")


def is_request_local(request: web.Request) -> bool:
    """Whether *request* was sent by a client running on this machine.

    PROTOCOL.md §7 makes the server the sole authority on "is the browser on
    the server's machine": a request's ``Host``/hostname cannot answer this,
    because a non-localhost hostname is ambiguous -- it looks identical
    whether ComfyUI is running with ``--listen 0.0.0.0`` and is browsed from
    the SAME machine via its own LAN address (e.g.
    ``http://192.168.1.23:8188``), or a genuinely different machine on the
    LAN is browsing it -- and those two cases demand opposite answers here.

    Instead this asks the operating system directly, via a throwaway
    ``bind()``: a process can only bind a socket to an address the machine
    actually owns (one of its own interfaces, or loopback) -- binding to any
    other address, including another host's address on the same LAN, fails
    with ``OSError`` (``WSAEADDRNOTAVAIL`` on Windows, still surfaced to
    Python as a plain ``OSError``, so no platform branching is needed). So
    "can this process bind to the request's peer address" is a
    deterministic, dependency-free test of "does this machine own that
    address" -- exactly "is the browser running on this machine" for the
    loopback-or-LAN peer address an HTTP client presents, including the
    same-machine-via-LAN-address case above that a hostname check gets
    wrong.

    A forwarding header (``X-Forwarded-For``/``X-Real-IP``) means
    ``request.remote`` is a reverse proxy's own address, not the real
    client's, so this fails safe and reports the request as non-local
    (PROTOCOL.md §2's 428 confirm flow).

    Args:
        request: The incoming aiohttp request.

    Returns:
        True if the request's peer address is one this machine can bind to
        (loopback, or an address owned by one of its own interfaces); False
        if a forwarding header is present, the peer address is unknown, or
        the bind test fails.
    """
    if any(header in request.headers for header in _FORWARDING_HEADERS):
        return False

    remote = request.remote
    if not remote:
        return False
    remote = _unmap_ipv4(remote)
    if _strip_zone(remote) in _LOOPBACK_ADDRESSES:
        return True
    return _can_bind(remote)


def _unmap_ipv4(address: str) -> str:
    """Reduce an IPv4-mapped IPv6 peer (``"::ffff:192.0.2.7"``) to plain IPv4.

    ComfyUI's ``--listen`` binds separate sockets per address (so v4 clients
    normally arrive as plain IPv4), but a single dual-stack socket in front
    of us (some proxies/containers) reports v4 peers in the mapped form --
    which would miss the loopback fast path and bind-test as the wrong
    family. Case-insensitive per RFC 4291 presentation forms.
    """
    lowered = address.lower()
    if lowered.startswith("::ffff:") and "." in address:
        return address[7:]
    return address


def _strip_zone(address: str) -> str:
    """Drop an IPv6 zone id suffix (e.g. ``"fe80::1%en0"`` -> ``"fe80::1"``).

    Only for string-equality/family checks -- :func:`_can_bind` still binds
    with the zone id intact where the OS needs it (see there).
    """
    return address.partition("%")[0]


def _can_bind(address: str) -> bool:
    """Attempt a throwaway UDP bind to *address*; True only if this machine owns it.

    *address* keeps its IPv6 zone id suffix (if any) intact on entry --
    unlike the string-comparison use of :func:`_strip_zone`, the zone id is
    required here: a link-local IPv6 address is only meaningful together
    with the interface it is scoped to, and passing it folded into the
    address string (``"fe80::1%en0"``) is rejected by ``bind()``, so it is
    resolved to a numeric scope id and passed as the 4-tuple's last element
    instead -- the "full tuple" a scoped bind needs.

    Args:
        address: A numeric IPv4 or IPv6 address, optionally suffixed with an
            IPv6 zone id (``"%en0"``, or the numeric ``"%3"`` Windows form).

    Returns:
        True if the bind succeeded (this machine owns *address*); False on
        any ``OSError`` (address not owned by this machine) or an
        unresolvable zone id.
    """
    bare_address, _, zone = address.partition("%")
    family = socket.AF_INET6 if ":" in bare_address else socket.AF_INET

    scope_id = 0
    if zone:
        try:
            scope_id = int(zone)
        except ValueError:
            try:
                scope_id = socket.if_nametoindex(zone)
            except OSError:
                logger.debug("Unresolvable IPv6 zone id %r in peer address %r", zone, address)
                return False

    sock_address = (
        (bare_address, 0, 0, scope_id) if family == socket.AF_INET6 else (bare_address, 0)
    )
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.bind(sock_address)
    except OSError as exc:
        logger.debug("Bind test failed for peer address %r: %s", address, exc)
        return False
    else:
        return True
    finally:
        sock.close()
