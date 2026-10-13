"""A seed whose apex has no A record should try www. before the site is written off.

Measured on the 92 catalogued sources holding zero pages — THREE OF FIVE sampled resolved only on
www, and every one of their crawls started at a hostname that does not exist:

    crye.com     DNS fail  |  www.crye.com    192.178.193.121
    adsys.com    DNS fail  |  www.adsys.com    50.59.127.126
    aisino.com   DNS fail  |  www.aisino.com  221.204.185.168  HTTP 405 (alive)

Confirmed real, not our resolver buckling under fan-out: three in-container tries and `getent` on
the host agreed exactly.

The idea already existed in `sitemap._host_variants()` ("many sites only publish on www") — seeds
never learned it. This is the narrow version: one lookup per seed, only on a clean name failure,
only ever ADDING www.
"""
from __future__ import annotations

import socket

import pytest

from crawler.async_engine import _seed_with_dns


@pytest.fixture()
def dns(monkeypatch):
    """Control resolution: `names` is the set that resolves; everything else raises gaierror."""
    def _go(names, exc=socket.gaierror):
        def _gai(host, *a, **k):
            if host in names:
                return [(2, 1, 6, "", ("1.2.3.4", 0))]
            raise exc("no address")
        monkeypatch.setattr(socket, "getaddrinfo", _gai)
    return _go


def test_www_is_used_when_the_apex_does_not_resolve(dns):
    dns({"www.crye.com"})
    assert _seed_with_dns("https://crye.com/") == "https://www.crye.com/"


def test_a_resolving_apex_is_left_exactly_alone(dns):
    dns({"aerodata.de", "www.aerodata.de"})
    assert _seed_with_dns("https://aerodata.de/") == "https://aerodata.de/"


def test_when_neither_resolves_the_original_seed_is_kept(dns):
    """So the failure is reported against the URL the catalogue actually holds, not a www variant
    nobody configured."""
    dns(set())
    assert _seed_with_dns("https://gone.example/") == "https://gone.example/"


def test_a_www_host_is_never_rewritten(dns):
    dns(set())
    assert _seed_with_dns("https://www.x.example/a") == "https://www.x.example/a"


def test_the_path_and_scheme_survive(dns):
    dns({"www.x.example"})
    assert _seed_with_dns("https://x.example/en/products?a=1") == \
        "https://www.x.example/en/products?a=1"


def test_a_bare_ip_is_left_alone(dns):
    dns(set())
    assert _seed_with_dns("http://10.0.0.5/x") == "http://10.0.0.5/x"


def test_a_resolver_error_that_is_not_a_name_failure_changes_nothing(monkeypatch):
    """A resolver buckling under fleet fan-out must NOT silently rewrite a working seed — that is
    the failure mode `source-liveness-audit` was written about, where a swamped resolver reported
    61 live sites as dead."""
    def _boom(*a, **k):
        raise OSError("resolver overloaded")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert _seed_with_dns("https://x.example/") == "https://x.example/"


def test_it_runs_at_seed_time_not_per_link():
    """One lookup per seed (a job has one or two). Putting this on the enqueue path would add a DNS
    round trip to every discovered link."""
    import inspect

    from crawler import async_engine
    src = inspect.getsource(async_engine.AsyncEngine._build_contexts) \
        if hasattr(async_engine.AsyncEngine, "_build_contexts") else inspect.getsource(async_engine)
    assert "_seed_with_dns(canonicalize_url(s))" in src
    assert "_seed_with_dns" not in inspect.getsource(async_engine.AsyncEngine._enqueue_links)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
