"""A seed probe is a point-in-time measurement, not a permanent verdict.

Measured the same afternoon the import-time probe shipped: `casic.com.cn` answered on
`http://www.casic.com.cn/` during the enrolment batch and returned nothing twenty minutes later on a
single re-check. The conservative design handled that correctly — it kept the existing seed rather
than clearing it — but it means a site that happened to be DOWN at enrolment keeps the conventional
`https://<apex>/` and nothing ever asks again. For the 92 zero-page sources that silence lasted
months, with 28 of them alive the whole time.

So the probe recurs. This file is about the three things that make a recurring probe safe and
useful rather than merely busy:

  * it only ever touches sites holding ZERO pages, so it cannot disturb a working crawl;
  * a failed probe NEVER clears a seed, so a bad network minute cannot erase an earlier correction;
  * a correction lands in BOTH places — the catalogue row and the schedule's frozen job JSON — and
    pulls `next_run` forward, because a fix that first takes effect in a week is not a fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mallory_orchestrator import api


class _R:
    def __init__(self, status, body, url=None):
        self.status_code, self.content = status, b"x" * body
        self.url = url or ""


class _Src:
    def __init__(self, domain, seed_urls=None):
        self.domain, self.seed_urls = domain, seed_urls
        self.category, self.last_crawled = "manufacturer_ir", None


class _Sched:
    def __init__(self, name, seed, next_run="LATER"):
        self.name, self.job, self.next_run = name, {"seed_urls": [seed]}, next_run
        self.enabled = True


class _DB:
    """Enough Session to drive seeds_recheck: scalars(select(X)) by model, get(Source, domain)."""

    def __init__(self, sources, scheds=()):
        self.sources, self.scheds, self.commits = list(sources), list(scheds), 0

    def scalars(self, stmt):
        from mallory_orchestrator.models import ScheduledJob
        want = stmt.column_descriptions[0]["entity"]
        rows = self.scheds if want is ScheduledJob else self.sources
        return type("_S", (), {"all": lambda _s, r=rows: list(r)})()

    def get(self, _model, domain):
        return next((s for s in self.sources if s.domain == domain), None)

    def commit(self):
        self.commits += 1


@pytest.fixture()
def web(monkeypatch):
    """Map url -> _R. Anything unmapped raises, like a dead host."""
    def _go(mapping):
        from mallory_orchestrator import seedprobe

        class _C:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, headers=None):
                v = mapping.get(url)
                if v is None:
                    raise OSError("no route")
                if not v.url:
                    v.url = url
                return v
        monkeypatch.setattr(seedprobe.httpx, "Client", _C)
    return _go


@pytest.fixture()
def rig(monkeypatch):
    """Pin the crawled set, the probe answers, and the clock."""
    def _go(sources, scheds=(), crawled=(), answers=None):
        monkeypatch.setattr(api, "_pages_by_domain",
                            lambda: {d: (99, 99_000) for d in crawled})
        monkeypatch.setattr(api.seedprobe, "resolve_many",
                            lambda doms, **kw: {d: (answers or {}).get(d) for d in doms})
        monkeypatch.setattr(api.scheduler, "utcnow", lambda: "NOW")
        api._SEED_RECHECK_AT = api._SETTLED_RECHECK_AT = api._SEED_RECHECK_TICKS = 0
        return _DB(sources, scheds)
    return _go


def test_only_sites_with_zero_pages_are_probed(rig):
    """THE safety property. A site with a working seed is by definition not in this set, so the
    action cannot re-point a crawl that is currently working."""
    seen = {}
    db = rig([_Src("alive.com"), _Src("dead.com")], crawled={"alive.com"})
    api.seedprobe.resolve_many = lambda doms, **kw: seen.setdefault("doms", list(doms)) and {}
    api.seeds_recheck(limit=25, apply=True, db=db)
    assert seen["doms"] == ["dead.com"]


def test_a_probe_that_finds_nothing_never_clears_a_seed(rig):
    """The guard the whole design rests on. A recheck running through a bad network minute would
    otherwise erase exactly the corrections an earlier pass made — the failure that once called 174
    live sites dead."""
    s = _Src("x.com", seed_urls=["http://www.x.com/"])
    db = rig([s], answers={"x.com": None})
    out = api.seeds_recheck(limit=25, apply=True, db=db)
    assert s.seed_urls == ["http://www.x.com/"]
    assert out["corrected"] == []
    assert db.commits == 0, "an empty pass still wrote to the database"


def test_a_site_that_answers_on_www_over_plain_http_is_re_pointed(rig):
    """norinco.com, casic.com.cn, cssc.net.cn — the shape a hardcoded https://<apex> can never
    reach."""
    s = _Src("norinco.com")
    db = rig([s], answers={"norinco.com": "http://www.norinco.com/"})
    out = api.seeds_recheck(limit=25, apply=True, db=db)
    assert s.seed_urls == ["http://www.norinco.com/"]
    assert out["applied"] == [{"domain": "norinco.com", "seed": "http://www.norinco.com/",
                               "moved": False}]


def test_the_schedules_frozen_seed_is_re_pointed_too(rig):
    """A ScheduledJob carries a COPY of the job JSON, and that copy — not the catalogue row — is the
    seed the scheduled crawl uses. Correcting only Source.seed_urls fixes the never-crawled dispatch
    path and leaves the weekly crawl pointed at the dead URL for ever."""
    s, sj = _Src("mbda-systems.com"), _Sched("mbda-systems.com", "https://mbda-systems.com/")
    db = rig([s], [sj], answers={"mbda-systems.com": "https://www.mbda-systems.com/"})
    api.seeds_recheck(limit=25, apply=True, db=db)
    assert sj.job["seed_urls"] == ["https://www.mbda-systems.com/"]


def test_a_corrected_seed_is_scheduled_to_take_effect_now(rig):
    """Weekly cadence means an uncorrected next_run defers the fix seven days — long enough for the
    next recheck to find the same site still at zero pages and 'correct' it again."""
    s, sj = _Src("renk.com"), _Sched("renk.com", "https://renk.com/", next_run="IN A WEEK")
    db = rig([s], [sj], answers={"renk.com": "https://www.renk.com/"})
    api.seeds_recheck(limit=25, apply=True, db=db)
    assert sj.next_run == "NOW"


def test_an_unrelated_schedule_is_left_alone(rig):
    s = _Src("renk.com")
    mine, theirs = _Sched("renk.com", "https://renk.com/"), _Sched("other.com", "https://other.com/")
    db = rig([s], [mine, theirs], answers={"renk.com": "https://www.renk.com/"})
    api.seeds_recheck(limit=25, apply=True, db=db)
    assert theirs.job["seed_urls"] == ["https://other.com/"]
    assert theirs.next_run != "NOW"


def test_an_answer_matching_the_conventional_seed_is_not_a_correction(rig):
    """The site is simply down or empty at https://<apex> — nothing to change, and recording it as a
    correction every fifteen minutes would make the history unreadable."""
    s = _Src("x.com")
    db = rig([s], answers={"x.com": "https://x.com/"})
    out = api.seeds_recheck(limit=25, apply=True, db=db)
    assert out["corrected"] == [] and s.seed_urls is None


def test_an_already_corrected_seed_is_not_corrected_again(rig):
    """Idempotence. Without it every pass re-reports the same fix and every pass drags next_run
    forward, re-crawling a site that has already been re-pointed and still returns nothing."""
    s = _Src("norinco.com", seed_urls=["http://www.norinco.com/"])
    sj = _Sched("norinco.com", "http://www.norinco.com/", next_run="LATER")
    db = rig([s], [sj], answers={"norinco.com": "http://www.norinco.com/"})
    out = api.seeds_recheck(limit=25, apply=True, db=db)
    assert out["corrected"] == []
    assert sj.next_run == "LATER"


# ── one page is not a crawl ───────────────────────────────────────────────────────────────────

def test_a_site_holding_a_single_page_is_treated_as_uncrawled(monkeypatch):
    """89 catalogued sites hold EXACTLY ONE page, and in every case it is the seed itself — the
    crawl stored the homepage and followed nothing, because the apex serves a redirect stub or a JS
    shell with no links in it.

    Under a has-any-pages test all 89 read as crawled. They are not: maxar.com now redirects to
    vantor.com (2.4 MB), atlas-elektronik.com to tkmsgroup.com, indracompany.com to indragroup.com
    (547 KB), patria.fi to patriagroup.com. Real companies, each represented by one holding page.
    """
    probed = []
    monkeypatch.setattr(api, "_pages_by_domain",
                        lambda: {"real.com": (412, 900_000), "onepage.com": (1, 80),
                                 "empty.com": (0, 0)})
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: probed.append(list(d)) or {})
    monkeypatch.setattr(api.scheduler, "utcnow", lambda: "NOW")
    api._SEED_RECHECK_AT = 0
    api.seeds_recheck(limit=25, apply=True,
                      db=_DB([_Src("real.com"), _Src("onepage.com"), _Src("empty.com")]))
    assert probed == [["empty.com", "onepage.com"]], "a one-page site was treated as crawled"


def test_a_moved_site_counts_the_pages_stored_under_its_NEW_domain(monkeypatch):
    """Measured right after the 25 moves were applied: wescam.com -> l3harris.com produced 1,453
    pages and pae.com -> amentum.com produced 1,026 — but every one landed under the NEW source_id,
    because the crawler stamps it from the URL it fetched. The catalogue keys still read one page.

    Counting only the catalogue key would put a site we have just crawled successfully back on the
    stuck list and re-probe it for ever."""
    moved = _Src("maxar.com", seed_urls=["https://vantor.com/"])
    monkeypatch.setattr(api, "_pages_by_domain",
                        lambda: {"maxar.com": (1, 80), "vantor.com": (296, 400_000)})
    probed = []
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: probed.append(list(d)) or {})
    api._SEED_RECHECK_AT = api._SETTLED_RECHECK_AT = api._SEED_RECHECK_TICKS = 0
    out = api.seeds_recheck(limit=25, apply=True, db=_DB([moved]))
    assert probed == [], "a successfully moved-and-crawled site is still being re-probed"
    assert out["zero_page_sites"] == 0


def test_a_settled_site_IS_eventually_probed_just_slowly(monkeypatch):
    """The gap the settled rule opened, and why excluding outright was wrong.

    "One page with real content" is TWO situations and the stored page cannot tell them apart:

        tisaaerospace.com   3,002 chars   a genuine one-page company site — finished
        faudi.com           3,941 chars   a landing page; the company is at faudi.de — MOVED

    Excluding both made the automatic path blind to exactly the case this action exists to catch.
    So settled sites stay in the rotation, on a slow lane."""
    probed = []
    monkeypatch.setattr(api, "_pages_by_domain", lambda: {"faudi.com": (1, 3941)})
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: probed.append(list(d)) or {})
    monkeypatch.setattr(api.scheduler, "utcnow", lambda: "NOW")
    api._SEED_RECHECK_AT = api._SETTLED_RECHECK_AT = api._SEED_RECHECK_TICKS = 0
    db = _DB([_Src("faudi.com")])

    for _ in range(api.SETTLED_RECHECK_EVERY):
        api.seeds_recheck(limit=25, apply=True, db=db)

    assert probed and probed[-1] == ["faudi.com"], "a settled site is never re-probed"
    assert sum(1 for b in probed if b) == 1, "a settled site is being probed every tick"


def test_the_stuck_set_is_probed_every_tick_not_on_the_slow_lane(monkeypatch):
    """The two lanes must stay separate: a site holding nothing is the urgent case."""
    probed = []
    monkeypatch.setattr(api, "_pages_by_domain",
                        lambda: {"dead.com": (0, 0), "finished.com": (1, 4000)})
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: probed.append(list(d)) or {})
    api._SEED_RECHECK_AT = api._SETTLED_RECHECK_AT = api._SEED_RECHECK_TICKS = 0
    db = _DB([_Src("dead.com"), _Src("finished.com")])
    for _ in range(3):
        api.seeds_recheck(limit=25, apply=True, db=db)
    assert all("dead.com" in b for b in probed), "the stuck site was skipped"
    assert not any("finished.com" in b for b in probed), "the settled site jumped the slow lane"


def test_the_headline_count_is_the_stuck_set_only(monkeypatch):
    """Folding finished sites into the number an operator watches would make a completed catalogue
    look like a permanent backlog."""
    monkeypatch.setattr(api, "_pages_by_domain",
                        lambda: {"dead.com": (0, 0), "finished.com": (1, 4000)})
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: {})
    api._SEED_RECHECK_AT = api._SETTLED_RECHECK_AT = api._SEED_RECHECK_TICKS = 0
    out = api.seeds_recheck(limit=25, apply=True,
                            db=_DB([_Src("dead.com"), _Src("finished.com")]))
    assert out["zero_page_sites"] == 1 and out["settled_sites"] == 1
    assert "1 site(s) stuck" in out["summary"]


def test_a_genuine_single_page_site_is_left_alone(monkeypatch):
    """Caught by the operator checking one by hand: tisaaerospace.com really is a one-page company
    site (3,002 chars of text, title "TISA website"). It is FINISHED, not stuck.

    Treating every one-page site as uncrawled re-probes it four times a rotation for as long as the
    catalogue exists and can never change anything. The text size is what separates the two:

        tisaaerospace.com     3,002 chars  finished
        kalashnikovgroup.ru      19 chars  a stub, genuinely stuck
    """
    probed = []
    monkeypatch.setattr(api, "_pages_by_domain",
                        lambda: {"tisaaerospace.com": (1, 3002),      # real page
                                 "kalashnikovgroup.ru": (1, 19)})     # stub
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: probed.append(list(d)) or {})
    api._SEED_RECHECK_AT = 0
    api.seeds_recheck(limit=25, apply=True,
                      db=_DB([_Src("tisaaerospace.com"), _Src("kalashnikovgroup.ru")]))
    assert probed == [["kalashnikovgroup.ru"]], \
        "a finished single-page site is being re-probed for ever"


def test_the_settled_threshold_is_the_one_the_probe_already_uses():
    """One number, one meaning. seedprobe rejects a response under _MIN_BYTES as 'not a page'; a
    stored page under the same size is 'not a crawl'. Two different constants would drift."""
    from mallory_orchestrator import seedprobe
    assert api.SETTLED_TEXT_CHARS == seedprobe._MIN_BYTES


def test_a_properly_crawled_site_is_never_re_probed(monkeypatch):
    """The guard that keeps this safe now that it touches sites which DO hold pages: two or more
    means links were followed, and its seed must not be second-guessed."""
    probed = []
    monkeypatch.setattr(api, "_pages_by_domain", lambda: {"fine.com": (2, 9_000)})
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: probed.append(list(d)) or {})
    api._SEED_RECHECK_AT = 0
    out = api.seeds_recheck(limit=25, apply=True, db=_DB([_Src("fine.com")]))
    assert probed == [] and out["checked"] == 0


def test_a_move_to_another_domain_is_PROPOSED_never_applied(rig):
    """THE guard, and it exists because the first live pass got it wrong.

    35 of 44 corrections were cross-domain and about eight were wrong in ways no status or size
    check can see: pgz.pl — the Polish Armaments Group — resolved to a perfume shop, sentekglobal to
    deloitte.com, rheintech.de to an ad-tracking redirect. An expired domain bought by an unrelated
    business answers 200 with a large, perfectly real page.

    A cross-domain re-point is a claim about who the company now IS. Same rule this module already
    states for moving a domain's ownership, applied to moving its identity."""
    moved, same = _Src("maxar.com"), _Src("teledyne.com")
    sj = _Sched("maxar.com", "https://maxar.com/", next_run="LATER")
    db = rig([moved, same], [sj],
             answers={"maxar.com": "https://vantor.com/",
                      "teledyne.com": "https://www.teledyne.com/en-us"})
    out = api.seeds_recheck(limit=25, apply=True, db=db)

    assert moved.seed_urls is None, "a cross-domain move was applied unattended"
    assert sj.job["seed_urls"] == ["https://maxar.com/"] and sj.next_run == "LATER"
    assert same.seed_urls == ["https://www.teledyne.com/en-us"], "a same-site fix was withheld"

    assert [c["domain"] for c in out["proposed"]] == ["maxar.com"]
    assert [c["domain"] for c in out["applied"]] == ["teledyne.com"]
    assert "awaiting review" in out["summary"]


def test_an_operator_can_accept_the_moves(rig):
    """Proposing forever would be its own failure — maxar.com -> vantor.com and
    wescam.com -> l3harris.com are real recoveries and need a way through."""
    s = _Src("maxar.com")
    db = rig([s], answers={"maxar.com": "https://vantor.com/"})
    api.seeds_recheck(limit=25, apply=True, apply_moves=True, db=db)
    assert s.seed_urls == ["https://vantor.com/"]


def test_a_subdomain_is_not_a_move(rig):
    """eu.glock.com and new.kspz.ru are the same company's own site. Flagging them for review
    alongside maxar.com -> vantor.com buries the real moves in noise."""
    s = _Src("glock.com")
    db = rig([s], answers={"glock.com": "https://eu.glock.com/en"})
    out = api.seeds_recheck(limit=25, apply=True, db=db)
    assert out["proposed"] == []
    assert s.seed_urls == ["https://eu.glock.com/en"]


# ── a for-sale page is not a recovery ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("parked", [
    "https://www.hugedomains.com/domain_profile.cfm?d=acmat.com",
    "https://acmat.com.sedoparking.com/",
    "https://dan.com/buy-domain/nexaero.com",
])
def test_a_domain_for_sale_page_is_refused(web, parked):
    """THE trap in widening this to one-page sites. A parking service serves a perfectly real
    3-44 KB page, so every size-based test passes — and nine catalogued 'manufacturers' now resolve
    to one (acmat, avontech, condorsystems, nexaero, shumerlya, sigmaas, spectrumastro,
    intelligentholding, projectq).

    Re-pointing at one is WORSE than leaving the seed broken: the crawl would then succeed and fill
    the corpus with sales copy under a defence company's name."""
    from mallory_orchestrator import seedprobe
    web({"https://acmat.com/": _R(200, 44_554, url=parked)})
    assert seedprobe.resolve("acmat.com") is None


def test_a_real_site_is_not_mistaken_for_a_parked_one(web):
    from mallory_orchestrator import seedprobe
    web({"https://vantor.com/": _R(200, 240_000, url="https://vantor.com/")})
    assert seedprobe.resolve("vantor.com") == "https://vantor.com/"


def test_the_parking_check_matches_subdomains_not_substrings():
    """`sedoparking.com` uses per-customer subdomains, so an exact-match list would miss every real
    case. And a substring test would condemn any host with 'dan.com' inside it."""
    from mallory_orchestrator import seedprobe
    assert seedprobe._is_parked("https://acmat.com.sedoparking.com/")
    assert seedprobe._is_parked("https://www.hugedomains.com/x")
    assert not seedprobe._is_parked("https://jordan.com/")
    assert not seedprobe._is_parked("https://sedona-defence.com/")


# ── "zero pages" must mean zero PAGES ─────────────────────────────────────────────────────────

def test_zero_page_means_the_store_holds_nothing_not_that_we_never_tried(monkeypatch):
    """THE bug that made the first version of this a permanent no-op.

    `_crawled_domains` answers a WIDER question — "have we been here" — by unioning
    Source.last_crawled with every SiteMeasurement row, so a site that was dispatched, failed and
    stored nothing counts as crawled. Run live against the real catalogue it reported **0 zero-page
    sites while 73 held nothing at all**, and the recheck probed no site ever.
    """
    monkeypatch.setattr(api, "_ingest_get", lambda p, **kw: [
        {"source_id": "haspages.com", "pages": 412},
        {"source_id": "www.tried-and-empty.com", "pages": 0}])
    have = api._domains_with_pages()
    assert have == {"haspages.com"}, "a site the store holds no pages for counted as crawled"


def test_an_unreachable_store_probes_nothing_rather_than_everything(monkeypatch):
    """An empty set would read as "no site holds any pages" and send the probe at all 690 — the
    fail-open shape that turns one ingest hiccup into a catalogue-wide scan."""
    def _boom(_p, **kw):
        raise RuntimeError("ingest unreachable")
    monkeypatch.setattr(api, "_ingest_get", _boom)
    assert api._domains_with_pages() is None

    probed = []
    monkeypatch.setattr(api.seedprobe, "resolve_many", lambda d, **kw: probed.append(d) or {})
    out = api.seeds_recheck(limit=25, apply=True, db=_DB([_Src("a.com")]))
    assert probed == [] and out["ok"] is False


# ── the rotating window ───────────────────────────────────────────────────────────────────────

def test_the_batch_is_bounded(rig):
    seen = {}
    db = rig([_Src("s%02d.com" % i) for i in range(60)])
    api.seedprobe.resolve_many = lambda doms, **kw: seen.setdefault("n", len(doms)) and {}
    api.seeds_recheck(limit=25, apply=True, db=db)
    assert seen["n"] == 25, "an unbounded probe of every zero-page site would run for minutes"


def test_consecutive_passes_probe_different_sites(rig):
    """Without a cursor the same 25 domains are re-probed for ever and the rest are never asked
    again — the exact silence this action exists to break, just with a shorter list."""
    seen = []
    db = rig([_Src("s%02d.com" % i) for i in range(60)])
    api.seedprobe.resolve_many = lambda doms, **kw: (seen.append(list(doms)), {})[1]
    api.seeds_recheck(limit=25, apply=True, db=db)
    api.seeds_recheck(limit=25, apply=True, db=db)
    assert seen[0] != seen[1]
    assert not set(seen[0]) & set(seen[1]), "the second pass re-probed the first pass's domains"


def test_the_cursor_wraps_and_covers_the_whole_list(rig):
    seen = []
    db = rig([_Src("s%02d.com" % i) for i in range(30)])
    api.seedprobe.resolve_many = lambda doms, **kw: (seen.append(list(doms)), {})[1]
    for _ in range(3):
        api.seeds_recheck(limit=12, apply=True, db=db)
    assert len({d for b in seen for d in b}) == 30, "some sites are never reached"


def test_a_dry_run_changes_nothing(rig):
    s, sj = _Src("renk.com"), _Sched("renk.com", "https://renk.com/")
    db = rig([s], [sj], answers={"renk.com": "https://www.renk.com/"})
    out = api.seeds_recheck(limit=25, apply=False, db=db)
    assert out["corrected"], "the preview reported nothing to do"
    assert s.seed_urls is None and sj.job["seed_urls"] == ["https://renk.com/"]
    assert db.commits == 0
    assert "would" in out["summary"]


def test_a_fully_crawled_catalogue_probes_nothing(rig):
    db = rig([_Src("a.com")], crawled={"a.com"})
    out = api.seeds_recheck(limit=25, apply=True, db=db)
    assert out["checked"] == 0 and out["zero_page_sites"] == 0


def test_the_recheck_uses_a_shorter_timeout_than_the_import():
    """Four shapes x twenty seconds x twenty-five domains is how a health action becomes a
    long-running job. A homepage that needs twenty seconds is not the case this recovers."""
    from mallory_orchestrator import seedprobe
    assert api.SEED_RECHECK_TIMEOUT_S < seedprobe._TIMEOUT
    assert "timeout" in Path(seedprobe.__file__).read_text(encoding="utf-8").split(
        "def resolve_many")[1].split("\ndef ")[0]


# ── wiring ────────────────────────────────────────────────────────────────────────────────────

def test_the_autopilot_runs_it_for_real(rig):
    """The whole point of the feature: nobody is going to press this every day."""
    from mallory_orchestrator import autopilot
    body = Path(autopilot.__file__).read_text(encoding="utf-8").split("def run_once")[1]
    assert "seeds_recheck" in body, "the recheck is a button nobody will press"
    assert "apply=True" in body.split("seeds_recheck")[1][:60], "the autopilot runs it dry"


def test_the_autopilot_never_accepts_a_move_on_its_own():
    """The blast-radius line. Unattended, this may fix a URL; it may not decide that one company is
    now another. pgz.pl -> a perfume shop is what that looks like when it goes wrong."""
    from mallory_orchestrator import autopilot
    body = Path(autopilot.__file__).read_text(encoding="utf-8").split("def run_once")[1]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "apply_moves=False" in code, "the autopilot may re-point a site onto another domain"
    assert "apply_moves=True" not in code


def test_the_autopilot_counts_applied_not_proposed():
    """Counting proposals as fixes would report 35 corrections a pass, for ever, while changing
    nothing — the history has to stay readable."""
    from mallory_orchestrator import autopilot
    body = Path(autopilot.__file__).read_text(encoding="utf-8").split("def run_once")[1]
    assert 'sd.get("applied")' in body


def test_it_runs_before_the_slow_read_only_action():
    """It writes. If a pass is interrupted the actions that change something must already have
    happened — the ordering rule this module already states for `assign`."""
    from mallory_orchestrator import autopilot
    body = Path(autopilot.__file__).read_text(encoding="utf-8").split("def run_once")[1]
    assert body.index("seeds_recheck") < body.index("data_quality")


def test_the_endpoint_is_registered():
    assert "/api/sources/seeds/recheck" in {r.path for r in api.router.routes}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
