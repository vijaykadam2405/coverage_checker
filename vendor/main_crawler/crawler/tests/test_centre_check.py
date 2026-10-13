"""The data-centre connection check: does each failure land on the right leg?

The whole value of this endpoint is that "the tunnel is down" and "your password is wrong" are
DIFFERENT answers with opposite fixes. If they collapse back into one "unreachable", the button is
no better than the status line it was added beside — so that separation is what is pinned here.

    python tests/test_centre_check.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest_api import live  # noqa: E402


def _legs(d):
    return {x["name"]: x for x in d["legs"]}


def test_unconfigured_is_not_a_fault():
    """A machine deliberately run standalone must not render as broken — ok is None, not False."""
    old = live.DC_DSN
    try:
        live.DC_DSN = ""
        d = live.centre_check()
        assert d["configured"] is False
        assert d["ok"] is None, f"unconfigured must be verdict-less, got ok={d['ok']!r}"
        assert "not broken" in d["note"]
        assert _legs(d)["configuration"]["ok"] is None
    finally:
        live.DC_DSN = old


def test_dead_tunnel_blames_the_tunnel():
    """An unroutable address = nothing accepts TCP. The tunnel leg must be the red one, and its
    fix must name db-tunnel — not the credentials, which are fine."""
    old = live.DC_DSN, live.DC_MINIO
    # Production waits generously on purpose (an SSH-tunnelled handshake is sometimes slow, and
    # this is a button someone pressed). A test must not: at production timeouts this single case
    # took 45s, because a dead address costs the full budget twice over on the retry.
    budgets = live.CHECK_TCP_TIMEOUT_S, live.CHECK_PG_TIMEOUT_S
    live.CHECK_TCP_TIMEOUT_S, live.CHECK_PG_TIMEOUT_S = 1.0, 1.0
    try:
        # TEST-NET-3 (RFC 5737): guaranteed unroutable, so this cannot accidentally reach a host.
        live.DC_DSN = "postgresql://u:p@203.0.113.9:5432/mallory"
        live.DC_MINIO = ""
        d = live.centre_check()
        legs = _legs(d)
        assert d["ok"] is False
        tun = legs["tunnel → postgres"]
        assert tun["ok"] is False, "a dead address must fail the tunnel leg"
        assert "db-tunnel" in tun["fix"], f"fix must point at the tunnel, got {tun['fix']!r}"
        assert legs["centre postgres"]["ok"] is False
        # The reassurance matters as much as the diagnosis: the reflex on seeing red here is to
        # assume pages are being lost.
        assert "Nothing is lost" in d["note"]
    finally:
        live.DC_DSN, live.DC_MINIO = old
        live.CHECK_TCP_TIMEOUT_S, live.CHECK_PG_TIMEOUT_S = budgets


def test_leg_shape_is_tri_state():
    """ok True / False / None are three different renderings on the page (green, red, grey), so a
    leg must never smuggle a not-configured state through as a failure."""
    assert live._leg("x", None, "d")["ok"] is None
    assert live._leg("x", False, "d", "fix")["fix"] == "fix"
    assert live._leg("x", True, "d")["ok"] is True


def test_tcp_probe_separates_open_from_shut():
    """The probe underneath the tunnel legs. Without a working negative it would report every host
    as reachable and the whole check would read green through a dead tunnel."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        ok, detail = live._tcp_open("127.0.0.1", port, timeout=3)
        assert ok is True, f"an open port must probe open: {detail}"
        assert str(port) in detail
    finally:
        srv.close()
    ok, detail = live._tcp_open("127.0.0.1", port, timeout=3)   # now closed
    assert ok is False, "a closed port must probe shut — a probe that never fails proves nothing"
    assert "127.0.0.1" in detail


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("  ok ", name)
    print("all centre-check tests passed")
