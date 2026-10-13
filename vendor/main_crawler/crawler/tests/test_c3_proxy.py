"""C3 + proxy: IP-block detection and the fresh-IP retry gate.

A stealth render that comes back as an Akamai "Access Denied" page must NOT count as a
served_by_camofox success — it's an IP-block, recognizable only from the 200-rendered body. The
detector drives (a) killing the false success and (b) triggering a fresh-IP retry (off by default)."""
import asyncio

from crawler import errors
from crawler.async_engine import AsyncEngine

_AKAMAI = ("<html><head>\n<title>Access Denied</title>\n</head><body>\n<h1>Access Denied</h1>\n"
           "You don't have permission to access \"http://www.war.gov/freedom250\" on this server.<p>\n"
           "Reference #18.281cc517.1784228296.201f39b4\n</p>"
           "<p>https://errors.edgesuite.net/18.281cc517.1784228296.201f39b4</p>\n</body></html>")


# --- detector -------------------------------------------------------------
def test_detects_akamai_block():
    assert errors.is_ip_block_page(_AKAMAI)
    # title alone is enough
    assert errors.is_ip_block_page("<html><head><title>Access Denied</title></head><body></body></html>")


def test_real_content_not_flagged():
    # a real article that happens to use ONE marker phrase must not trip (needs >=2, or the title)
    article = ("<html><head><title>Army denies report on new howitzer</title></head><body>"
               "<p>Officials said access denied to the range was temporary.</p></body></html>")
    assert not errors.is_ip_block_page(article)
    assert not errors.is_ip_block_page("")
    assert not errors.is_ip_block_page(None)


# --- retry gate -----------------------------------------------------------
def test_retry_off_by_default(monkeypatch):
    # No CRAWLER_C3_PROXY_RETRY → budget 0 → _c3_fresh_ip_retry is a no-op returning None WITHOUT
    # calling camofox (so a blocked render just fails honestly when no proxy is wired).
    monkeypatch.delenv("CRAWLER_C3_PROXY_RETRY", raising=False)
    eng = AsyncEngine.__new__(AsyncEngine)
    assert eng._c3_proxy_retry_budget() == 0

    import crawler.async_engine as ae
    called = {"n": 0}
    monkeypatch.setattr(ae.camofox_client, "render",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or None)

    class _Item:
        url = "https://x/y"
        class ctx:  # noqa: N801
            class job:  # noqa: N801
                job_id = "t"
    out = asyncio.run(eng._c3_fresh_ip_retry(_Item(), 30.0, False))
    assert out is None and called["n"] == 0        # never touched camofox


def test_retry_budget_on(monkeypatch):
    monkeypatch.setenv("CRAWLER_C3_PROXY_RETRY", "1")
    monkeypatch.setenv("CRAWLER_C3_PROXY_RETRIES", "3")
    eng = AsyncEngine.__new__(AsyncEngine)
    assert eng._c3_proxy_retry_budget() == 3
