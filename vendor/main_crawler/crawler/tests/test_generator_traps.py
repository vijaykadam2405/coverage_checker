"""URLs a site's software MINTS, rather than pages a site has.

Written against a fleet that had stored two pages in an hour at load average 79. Four instances
held 128 of 128 render-lane slots with zero renders actually in flight; the queue behind them was
calendar exports, each burning the 240s item watchdog and requeueing itself. 4,704 of 19,014
pending URLs were `?ical=`, and the corpus separately held 22,823 pages whose bytes already existed
under a different URL — 3,304 of those from ONE login form with a `?back=` parameter.

The distinction that matters, and the reason these get a rule instead of a bigger cap: a facet
explosion is finite and mostly harmless, whereas nothing here can EVER be content. Retrying an
.ics download four times through a browser is pure loss.

Every URL below is real, taken from the frontiers and the corpus.
"""
from __future__ import annotations

import re

import pytest

from crawler.async_engine import _is_action_link, _looks_paginated


# ── what must be refused ──────────────────────────────────────────────────────────────────────

CALENDAR = [
    # gdbiw.com (General Dynamics Bath Iron Works) — 5,445 pending, 4,690 of them these.
    "https://www.gdbiw.com/events/category/community-events/day/2026-02-11/?ical=1",
    "https://www.gdbiw.com/events/category/hiring-events/day/2026-06-16/?outlook-ical=1",
    "https://www.gdbiw.com/events/2026-06-29/?outlook-ical=1",
    "https://www.gdbiw.com/events/category/employee-events/best-events/hikes/day/2026-10-30/?outlook-ical=1",
    # The nav that generates them.
    "https://www.gdbiw.com/events/category/community-events/day/2026-12-08/",
    "https://www.gdbiw.com/events/?tribe-bar-date=2027-03-01",
    "https://www.gdbiw.com/events/?eventDisplay=month&eventDate=2029-01",
]

LOGIN_RECURSION = [
    # zentauron.de — 78 distinct URLs under a single content hash, 3,304 redundant pages stored.
    "https://www.zentauron.de/de/anmeldung?back=https%3A%2F%2Fwww.zentauron.de%2Fde%2F2877-.html",
    "https://www.zentauron.de/es/iniciar-sesion?back=https%3A%2F%2Fwww.zentauron.de%2Fde%2F",
    # The same control in the other languages a European supplier site ships.
    "https://x.example/fr/connexion",
    "https://x.example/it/accedi?redirect=https://x.example/it/",
    "https://x.example/account?returnUrl=https%3A%2F%2Fx.example%2Fcart",
]


@pytest.mark.parametrize("url", CALENDAR + LOGIN_RECURSION)
def test_generator_urls_are_refused(url):
    assert _is_action_link(url), url


# ── what must still be crawled ────────────────────────────────────────────────────────────────
# The whole risk of a junk rule is over-reach, and over-reach here is invisible: a page that is
# never fetched leaves no trace anywhere. Each of these is a real URL from the same frontiers.

KEEP = [
    # Real content sitting in the same queues as the traps above.
    "https://plk.group-kremny.ru/catalog/silovye-moduli-catalog/silovye-moduli/silovoy-modul-mtki-400-17-62",
    "https://rostec.ru/media/news/noveyshiy-dvigatel-nk-36st-32-razrabotki-odk-voshel-v-top-5",
    "https://shvabe.com/en/press/news/shvabe-concluded-an-agreement-with-bionet-south-korea",
    "https://www.godrejenterprises.com/newsroom/newsroom/podcast/ai-with-purpose",
    # A date in a path is not a calendar: news sites publish under one constantly.
    "https://example.com/news/2026-07-31/new-frigate-delivered",
    "https://example.com/2026/07/31/new-frigate-delivered",
    # `back`/`next`/`return` as ordinary values. The rule requires the value to LOOK like a URL,
    # precisely so a flag or an id keeps its page.
    "https://example.com/page?back=1",
    "https://example.com/results?next=t3_abc123",          # an opaque cursor, not a redirect
    "https://example.com/orders?return=refund",
    # Substring collisions with the login words: `entrar` and `accedi` are short and appear inside
    # ordinary Spanish/Italian paths, so the rule anchors on a whole path segment.
    "https://example.com/es/centrar-el-foco",
    "https://example.com/productos/entrada-de-aire",
    "https://example.com/it/accessori/ottica",
    # `ics` and `ical` inside a longer word or a path — the rule matches a QUERY PARAM, not text.
    "https://example.com/physics=1",
    "https://example.com/avionics/systems",
    "https://example.com/technical-support",
]


@pytest.mark.parametrize("url", KEEP)
def test_real_pages_are_still_crawled(url):
    assert not _is_action_link(url), url


def test_pagination_is_untouched():
    """Following a listing chain is usually the POINT of a crawl; these must keep their fast lane."""
    for u in ("https://x.com/list?page=2", "https://x.com/blog/page/3",
              "https://shvabe.com/en/press/news/some-article?PAGEN_1=9"):
        assert not _is_action_link(u), u
    assert _looks_paginated("https://x.com/list?page=2")


# ── the purge must agree with the filter ──────────────────────────────────────────────────────

def test_the_frontier_purge_uses_the_enqueue_predicate(tmp_path, monkeypatch):
    """A purge with its own idea of junk deletes URLs the crawler then re-discovers, for ever.

    So this asserts the wiring, not a second copy of the rules: what the endpoint prunes is exactly
    what _is_action_link rejects.
    """
    monkeypatch.setenv("CRAWLER_DATA_DIR", str(tmp_path))
    from crawler.dedup import CrawlHistory

    h = CrawlHistory(db_path=tmp_path / "h.sqlite")
    try:
        rows = [(u, 1, "https://seed/", 0) for u in CALENDAR + LOGIN_RECURSION + KEEP]
        h.frontier_add("job1", rows)

        dry = h.frontier_prune(_is_action_link)
        assert dry["junk"] == len(CALENDAR) + len(LOGIN_RECURSION)
        assert dry["applied"] is False
        assert h.frontier_counts("job1")["pending"] == len(rows), "a dry run wrote something"

        got = h.frontier_prune(_is_action_link, apply=True)
        assert got["junk"] == dry["junk"] and got["applied"] is True
        left = {u for u, *_ in h.frontier_pending("job1")}
        assert left == set(KEEP), "the purge and the enqueue filter disagree"
        assert "gdbiw.com" in got["by_host"], got["by_host"]
    finally:
        h.close()


def test_the_purge_leaves_the_seen_set_alone(tmp_path, monkeypatch):
    """`done` rows are what stop a URL being queued a second time. Delete those and every trap
    walks straight back in on the next page that links to it."""
    monkeypatch.setenv("CRAWLER_DATA_DIR", str(tmp_path))
    from crawler.dedup import CrawlHistory

    h = CrawlHistory(db_path=tmp_path / "h2.sqlite")
    try:
        junk = CALENDAR[0]
        h.frontier_add("job1", [(junk, 1, None, 0), (KEEP[0], 1, None, 0)])
        h.frontier_done("job1", [junk])                     # already fetched once
        assert h.frontier_prune(_is_action_link, apply=True)["junk"] == 0
        assert junk in h.frontier_seen("job1"), "the purge dropped a seen-set entry"
    finally:
        h.close()


def test_a_running_batch_refuses_the_prune():
    """Same reason /v1/resumable/clear refuses: those rows are the live crawl's resume point."""
    from fastapi.testclient import TestClient

    from crawler_api import app as appmod

    c = TestClient(appmod.app)
    appmod._batch_status["running"] = True
    try:
        assert c.post("/v1/frontier/prune", json={"apply": True}).status_code == 409
        # A dry run is a read and stays allowed — that is how an operator decides to stop.
        assert c.post("/v1/frontier/prune", json={"apply": False}).status_code == 200
    finally:
        appmod._batch_status["running"] = False


def test_the_regex_compiles_and_is_anchored():
    """A stray escape here silently turns an alternation into a literal — this exact file already
    carries one such scar in _ACTION_LINK_RE's history."""
    from crawler.async_engine import _ACTION_LINK_RE, _GENERATOR_TRAP_RE
    for rx in (_ACTION_LINK_RE, _GENERATOR_TRAP_RE):
        assert isinstance(rx, re.Pattern) and rx.flags & re.I
    assert not _ACTION_LINK_RE.search("https://x.com/(login")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
