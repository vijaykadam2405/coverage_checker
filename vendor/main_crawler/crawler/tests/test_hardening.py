"""Self-checks for the crawler-hardening helpers (Scrapling-derived learnings).

Assert-based, no fixtures — run directly:  python cralwer/tests/test_hardening.py
Covers the pure logic (tracker match, DoH/stealth arg assembly, dev-cache round-trip). The
network/browser paths (_impersonate_fetch, the init script) are exercised by the manual E2E.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # cralwer/ on the path

from crawler import stealth
from crawler.fetcher import FetchResult, _dev_cache_get, _dev_cache_put


def _clear(*names):
    for n in names:
        os.environ.pop(n, None)


def test_is_tracker():
    assert stealth.is_tracker("www.google-analytics.com")
    assert stealth.is_tracker("google-analytics.com")
    assert stealth.is_tracker("googletagmanager.com")
    assert stealth.is_tracker("static.hotjar.com")
    assert not stealth.is_tracker("mbda-systems.com")
    assert not stealth.is_tracker("mod.gov.my")
    assert not stealth.is_tracker("sipri.org")
    assert not stealth.is_tracker("")


def test_doh_launch_args():
    _clear("CRAWLER_DOH_TEMPLATE")
    assert stealth.doh_launch_args() == []
    assert stealth.doh_url() is None
    os.environ["CRAWLER_DOH_TEMPLATE"] = "https://cloudflare-dns.com/dns-query"
    args = stealth.doh_launch_args()
    assert any("dns-over-https-mode=secure" in a for a in args)
    assert any("cloudflare-dns.com/dns-query" in a for a in args)
    _clear("CRAWLER_DOH_TEMPLATE")


def test_stealth_toggle():
    _clear("CRAWLER_STEALTH")
    assert "--disable-blink-features=AutomationControlled" in stealth.stealth_launch_args()
    assert stealth.stealth_ignore_default_args() == ["--enable-automation"]
    os.environ["CRAWLER_STEALTH"] = "0"
    assert stealth.stealth_launch_args() == []
    assert stealth.stealth_ignore_default_args() == []
    _clear("CRAWLER_STEALTH")


def test_dev_cache_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CRAWLER_DEV_CACHE_DIR"] = d
        # HTML page
        r = FetchResult(url="http://x.test/a", final_url="http://x.test/a", status=200,
                        content_type="text/html", kind="html", text_html="<h1>hi</h1>",
                        etag="W/\"abc\"", fetched_at="t")
        _dev_cache_put("http://x.test/a", r)
        got = _dev_cache_get("http://x.test/a")
        assert got is not None
        assert got.status == 200 and got.kind == "html"
        assert got.text_html == "<h1>hi</h1>" and got.etag == "W/\"abc\""
        # Binary (pdf) page — bytes survive the round-trip
        rb = FetchResult(url="http://x.test/b.pdf", final_url="http://x.test/b.pdf", status=200,
                         content_type="application/pdf", kind="pdf", body_bytes=b"%PDF-1.7 xyz",
                         fetched_at="t")
        _dev_cache_put("http://x.test/b.pdf", rb)
        gotb = _dev_cache_get("http://x.test/b.pdf")
        assert gotb is not None and gotb.kind == "pdf" and gotb.body_bytes == b"%PDF-1.7 xyz"
        # Miss returns None
        assert _dev_cache_get("http://x.test/never") is None
    _clear("CRAWLER_DEV_CACHE_DIR")


def test_host_overrides():
    from crawler.async_engine import _parse_host_overrides, _match_override
    m = _parse_host_overrides("sipri.org=3, mod.gov.my=1 , bad, x=0, y=-2, .foo.com=4")
    assert m == {"sipri.org": 3, "mod.gov.my": 1, "foo.com": 4}, m   # drops bad/x=0/y=-2, strips dot
    assert _match_override("www.sipri.org", m) == 3
    assert _match_override("sipri.org", m) == 3
    assert _match_override("www.mod.gov.my", m) == 1
    assert _match_override("rtx.com", m) is None
    assert _match_override("evilsipri.org", m) is None   # segment-suffix, not substring
    assert _match_override("", m) is None


def main():
    tests = [test_is_tracker, test_doh_launch_args, test_stealth_toggle, test_dev_cache_roundtrip,
             test_host_overrides]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\nall {len(tests)} hardening self-checks passed")


if __name__ == "__main__":
    main()
