"""Unit tests for cpsb.locality.is_request_local (PROTOCOL.md §7).

``is_request_local`` only ever reads ``request.headers`` (a mapping
supporting ``in``) and ``request.remote`` (a string or ``None``), so a
minimal stand-in is used here instead of a real aiohttp request -- the
route-level wiring (the actual 428 gate at ``POST /cpsb/open``) is covered
separately in ``test_routes.py``.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from cpsb.locality import is_request_local


def fake_request(remote: str | None, headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(remote=remote, headers=headers or {})


class TestIsRequestLocal:
    def test_loopback_ipv4_is_local(self):
        assert is_request_local(fake_request("127.0.0.1")) is True

    def test_loopback_ipv6_is_local(self):
        assert is_request_local(fake_request("::1")) is True

    def test_non_local_address_is_not_local(self):
        # 203.0.113.0/24 is TEST-NET-3 (RFC 5737): reserved for
        # documentation, guaranteed never actually assigned to a real
        # machine's interface, so the bind test must always fail here.
        assert is_request_local(fake_request("203.0.113.7")) is False

    def test_none_remote_is_not_local(self):
        assert is_request_local(fake_request(None)) is False

    def test_forwarding_header_present_denies_even_loopback(self):
        """A proxied request's ``request.remote`` is the proxy's own
        address, not the real client's -- locality is unknowable, so this
        fails safe to False regardless of what that address happens to be.
        """
        assert is_request_local(fake_request("127.0.0.1", {"X-Forwarded-For": "1.2.3.4"})) is False
        assert is_request_local(fake_request("127.0.0.1", {"X-Real-IP": "1.2.3.4"})) is False

    def test_ipv4_mapped_loopback_is_local(self):
        """A dual-stack socket reports IPv4 peers as ``::ffff:a.b.c.d`` --
        the mapped form must reduce to plain IPv4 before the loopback fast
        path and bind test (which would otherwise run in the wrong family).
        """
        assert is_request_local(fake_request("::ffff:127.0.0.1")) is True
        assert is_request_local(fake_request("::FFFF:127.0.0.1")) is True

    def test_ipv4_mapped_non_local_is_not_local(self):
        assert is_request_local(fake_request("::ffff:203.0.113.7")) is False

    def test_real_lan_address_is_local(self):
        """This machine's own LAN-facing address must also read as local --
        the exact case PROTOCOL.md §7 calls out that a hostname check gets
        wrong. Discovered via the standard UDP ``connect()`` trick (picks a
        local route/interface; sends no actual packets). Skipped, not
        failed, when this environment has no routable interface at all.
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            lan_ip = probe.getsockname()[0]
        except OSError:
            pytest.skip("No routable network interface available in this environment")
        finally:
            probe.close()

        assert is_request_local(fake_request(lan_ip)) is True
