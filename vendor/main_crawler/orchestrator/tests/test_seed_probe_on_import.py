"""A site must not enter the catalogue with a seed nobody has confirmed answers.

Every seed was built as `https://<apex>/`. Probing all four shapes (https/http x apex/www) across
the 92 catalogued sources holding zero pages found that **28 were alive and simply had the wrong
URL** — and not obscure ones:

    mbda-systems.com   https://apex refused  ->  https://www.mbda-systems.com/   569 KB
    norinco.com                              ->  http://www.norinco.com/
    casic.com.cn                             ->  http://www.casic.com.cn/
    almaz-antey.ru                           ->  http://almaz-antey.ru/

Chinese and Russian defence sites commonly serve plain HTTP on www only. The crawl reported
`fetched=0` for perfectly healthy sites, for months.

So the probe moved to enrolment. This file is about it staying honest there: preferring TLS,
refusing error pages, and — above all — never guessing a URL into the catalogue when the network is
having a bad minute.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mallory_orchestrator import seedprobe


class _R:
    def __init__(self, status, body, url=None):
        self.status_code, self.content = status, b"x" * body
        self.url = url or ""


@pytest.fixture()
def web(monkeypatch):
    """Map url -> _R (or an Exception to raise). Anything unmapped raises, like a dead host."""
    def _go(mapping):
        class _C:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, headers=None):
                v = mapping.get(url)
                if v is None:
                    raise OSError("no route")
                if isinstance(v, Exception):
                    raise v
                if not v.url:
                    v.url = url
                return v
        monkeypatch.setattr(seedprobe.httpx, "Client", _C)
    return _go


def test_the_four_shapes_are_tried_tls_and_apex_first():
    assert seedprobe.variants("x.com") == [
        "https://x.com/", "https://www.x.com/", "http://x.com/", "http://www.x.com/"]


def test_www_only_over_https_is_found(web):
    """mbda-systems.com — the apex refuses, www serves 569 KB."""
    web({"https://www.mbda-systems.com/": _R(200, 569_000)})
    assert seedprobe.resolve("mbda-systems.com") == "https://www.mbda-systems.com/"


def test_plain_http_on_www_is_found(web):
    """norinco.com and casic.com.cn — no TLS at all, www only. The shape a `https://<apex>` seed
    can never reach."""
    web({"http://www.norinco.com/": _R(200, 19_525)})
    assert seedprobe.resolve("norinco.com") == "http://www.norinco.com/"


def test_https_apex_wins_when_several_answer(web):
    """A site reachable both ways should be crawled over TLS, at the address the catalogue is
    keyed on."""
    web({"https://x.com/": _R(200, 9000), "https://www.x.com/": _R(200, 9000),
         "http://x.com/": _R(200, 9000)})
    assert seedprobe.resolve("x.com") == "https://x.com/"


def test_the_post_redirect_url_is_returned(web):
    """aerodata.de lands on https://www.aerodata.de/en/ — seeding the pre-redirect URL wastes a hop
    and, on a locale redirect, can land somewhere the scope rules then reject."""
    web({"https://aerodata.de/": _R(200, 160_030, url="https://www.aerodata.de/en/")})
    assert seedprobe.resolve("aerodata.de") == "https://www.aerodata.de/en/"


@pytest.mark.parametrize("status", [404, 503, 405, 400])
def test_an_error_page_is_not_an_answer(web, status):
    """amz.ru 404s, aerolloy.com 503s, aisino.com 405s. A server talking is not a site to crawl —
    accepting one hands the crawler a seed that yields exactly one error page."""
    web({"https://x.com/": _R(status, 9000)})
    assert seedprobe.resolve("x.com") is None


def test_a_redirect_stub_is_not_an_answer(web):
    web({"https://x.com/": _R(200, 40)})
    assert seedprobe.resolve("x.com") is None


def test_nothing_answering_returns_none_not_a_guess(web):
    """THE guard. None means the caller keeps the conventional https://<apex>/ — a probe run while
    the network is having a bad minute must never permanently rewrite a good seed. That mistake,
    at 24 concurrent workers, once called 174 sites dead when 61 were live."""
    web({})
    assert seedprobe.resolve("x.com") is None


def test_the_batch_probe_is_not_a_thundering_herd():
    src = Path(seedprobe.__file__).read_text(encoding="utf-8")
    assert "workers: int = 6" in src, "the batch probe would swamp the resolver"


def test_a_bad_cert_is_not_a_dead_site():
    """An expired certificate is a fact about the site; the crawler decides per host whether to
    proceed. This probe only answers 'is anything there'."""
    src = Path(seedprobe.__file__).read_text(encoding="utf-8")
    assert "verify=False" in src


# ── wiring ────────────────────────────────────────────────────────────────────────────────────

def test_import_stores_the_verified_seed_and_never_fails_on_it():
    from mallory_orchestrator import api
    src = Path(api.__file__).read_text(encoding="utf-8")
    body = src.split("def import_commit")[1].split("\n@router")[0]
    assert "seedprobe.resolve_many(added)" in body, "import does not verify seeds"
    assert "s.seed_urls = [url]" in body
    assert "except Exception" in body, "a probe failure must not fail the import"
    assert "seeds_corrected" in body, "the operator is not told how many were corrected"


def test_the_job_builder_prefers_the_verified_seed():
    """Storing it and not using it changes nothing — the shape of every unwired fix in this repo."""
    from mallory_orchestrator import api
    src = Path(api.__file__).read_text(encoding="utf-8")
    assert '(s.seed_urls or ["https://%s/" % s.domain])' in src


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
