"""Defence-only tender scoping + the disposable-inbox login path.

Two features, both pinned without a network or a browser: the parts that decide WHAT gets crawled
and kept are pure functions, and those are the parts that fail silently when they regress (a
crawl that quietly keeps everything looks exactly like a crawl that is working).

Runnable directly:  python tests/test_tender_defense.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import gate, siteauth, tempmail, tenderdefense  # noqa: E402
from crawler.harvest import _link_is_relevant  # noqa: E402
from crawler.models import Job  # noqa: E402


def _job(**kw) -> Job:
    kw.setdefault("job_id", "t1")
    kw.setdefault("job_type", "tender")
    kw.setdefault("seed_urls", ["https://www.tender18.com/all-india-tenders"])
    return Job(**kw)


# ── the profile turns itself on ──────────────────────────────────────────────────────────────
def t_a_tender_job_defends_itself_without_being_asked() -> None:
    """The whole point of the feature: the operator types job_type=tender and gets a defence
    crawl. If this stops firing, the batch silently comes back full of road-works tenders."""
    j = _job()
    assert j.require_keywords, "tender job got no defence keep-filter"
    assert "ordnance" in j.require_keywords and "defence" in j.require_keywords
    assert j.link_relevance_keywords == j.require_keywords
    assert j.max_pdfs_per_page == 0, "a tender's attachments must not be capped at 3"
    assert j.auth is True, "tender18 is a gated portal — login should auto-enable"


def t_other_job_types_are_untouched() -> None:
    """The profile is scoped to tenders. A news job that suddenly required defence keywords would
    silently zero every news crawl in the batch."""
    j = Job(job_id="n1", job_type="news", seed_urls=["https://example.com/"])
    assert j.require_keywords == []
    assert j.link_relevance_keywords == []
    assert j.auth is None
    assert j.max_pdfs_per_page is None


def t_the_operator_can_still_override_every_knob() -> None:
    """Presets are defaults, not mandates — the same rule _apply_hunt_mode follows. An explicit
    empty list must mean "no filter", not "you forgot to set it, here are 90 keywords"."""
    j = _job(require_keywords=[], link_relevance_keywords=["frigate"], auth=False,
             max_pdfs_per_page=5)
    assert j.require_keywords == []
    assert j.link_relevance_keywords == ["frigate"]
    assert j.auth is False and j.max_pdfs_per_page == 5


def t_auth_stays_off_for_an_ungated_tender_portal() -> None:
    """Most government procurement portals need no account. Auto-registering on one would create
    an account nobody asked for on a .gov site."""
    j = _job(seed_urls=["https://eprocure.gov.in/epublish/app"])
    assert j.auth is None, "auth auto-enabled on a portal that is not in GATED_PORTALS"
    assert j.require_keywords, "...but the defence filter still applies"


# ── the keep gate ────────────────────────────────────────────────────────────────────────────
class _KP:
    """Stand-in for the global corpus trie: len() and extract_keywords, nothing else."""

    def __init__(self, words=()):
        self.words = list(words)

    def __len__(self):
        return len(self.words)

    def extract_keywords(self, text):
        low = text.lower()
        return [w for w in self.words if w.lower() in low]


def t_a_civil_tender_is_dropped_even_though_the_corpus_is_empty() -> None:
    """An empty corpus normally fails OPEN (keep-all) so a bad keyword-file path can't zero the
    crawl. That escape hatch must not apply to a defence-only job, or the mode does nothing on
    exactly the deployments most likely to be misconfigured."""
    j = _job()
    r = gate.evaluate(j, "Tender For High Visability Reflective Vest (Q3)",
                      "Supply of reflective safety vests to the municipal corporation.",
                      None, _KP())
    assert r.keep is False and r.reason == "no_required_keyword"


def t_a_defence_tender_is_kept_with_no_watchlist_hit() -> None:
    """require_keywords REPLACES the corpus gate rather than AND-ing with it. A real Ordnance
    Factory tender names no watchlist company, so AND would drop precisely what we came for."""
    j = _job()
    r = gate.evaluate(j, "Supply Of Ammunition Boxes To Ordnance Factory Khamaria",
                      "Sealed tenders are invited by the Ordnance Factory Board.", None, _KP())
    assert r.keep is True and r.reason == "required_keyword_match"
    assert "ordnance" in [m.lower() for m in r.matched_keywords]


def t_a_watchlist_hit_is_still_recorded_alongside() -> None:
    j = _job()
    r = gate.evaluate(j, "Radar spares for Bharat Electronics", "Naval radar overhaul.",
                      None, _KP(["Bharat Electronics"]))
    low = [m.lower() for m in r.matched_keywords]
    assert r.keep is True and "bharat electronics" in low and "radar" in low


def t_keyword_false_cannot_smuggle_a_civil_page_through() -> None:
    """keyword=False means "crawl the whole site" — a budget switch. It must not also disable the
    defence requirement, or `keyword:false` on a tender job returns the entire portal."""
    j = _job(keyword=False)
    r = gate.evaluate(j, "Repair of school boundary wall", "Civil works tender.", None, _KP())
    assert r.keep is False and r.reason == "no_required_keyword"


def t_freshness_still_runs_first() -> None:
    j = _job(freshness_days=1)
    r = gate.evaluate(j, "Ordnance Factory ammunition", "x", "2001-01-01T00:00:00Z", _KP())
    assert r.keep is False and r.reason == "stale_beyond_freshness_days"


# ── term-list precision (the expensive kind of bug) ──────────────────────────────────────────
def t_the_terms_do_not_match_common_civil_tenders_as_substrings() -> None:
    """_link_is_relevant matches SUBSTRINGS, not word boundaries. A term like "arms" or "tank"
    would hit pharmaceuticals and water tanks — on a portal where >99% of listings are civil,
    that is the difference between a crawl that finishes and one that never does."""
    civil = [
        "Tender For Construction Of Overhead Water Tank At Village Panchayat",
        "Supply Of Pharmaceuticals And Surgical Disposables To District Hospital",
        "Annual Maintenance Contract For Air Conditioners In Municipal Office",
        "Providing Cement Mortar Plastering Work For School Building",
        "Hiring Of Vehicles For Revenue Department On Monthly Rental Basis",
        "Tender For High Visability Reflective Vest (Q3)",
        "Supply Of Furniture And Almirah For Government College",
        "Procurement Of Solar Street Light Poles For Gram Panchayat",
    ]
    for title in civil:
        assert not _link_is_relevant(title, tenderdefense.TERMS), \
            f"false positive — a civil tender would be fetched: {title!r}"


def t_the_terms_do_match_real_defence_tenders() -> None:
    for title in [
        "Supply Of Ammunition Boxes To Ordnance Factory Khamaria",
        "Repair And Overhaul Of Radar System At Naval Dockyard Visakhapatnam",
        "Procurement Of Night Vision Devices For Indian Army",
        "Tender For Catering Services At Sainik School Korukonda",
        "Construction Of Married Accommodation In Cantonment Area",
        "Supply Of Spares To Hindustan Aeronautics Limited Nashik Division",
        "AMC Of Air Conditioners At Ministry Of Defence Headquarters",
        "Hiring Of Vehicles For Border Security Force Frontier Headquarters",
    ]:
        assert _link_is_relevant(title, tenderdefense.TERMS), \
            f"missed a real defence tender: {title!r}"


def t_gated_portal_detection_covers_subdomains_but_not_lookalikes() -> None:
    assert tenderdefense.is_gated_portal("https://www.tender18.com/x")
    assert tenderdefense.is_gated_portal("https://m.bidassist.com/")
    assert not tenderdefense.is_gated_portal("https://nottender18.com/")
    assert not tenderdefense.is_gated_portal("https://tender18.com.evil.net/")


def t_host_of_accepts_a_bare_hostname() -> None:
    """Session lookups pass a bare host; crawls pass a URL. urlsplit returns hostname=None for the
    scheme-less form, which would key every stored session under ""."""
    assert tenderdefense.host_of("tender18.com") == "tender18.com"
    assert tenderdefense.host_of("https://www.tender18.com/a/b") == "tender18.com"
    assert tenderdefense.host_of("") == ""


# ── login plumbing ───────────────────────────────────────────────────────────────────────────
def t_form_fields_are_classified_by_intent_not_by_order() -> None:
    assert siteauth.classify_field("txtEmailId", "text") == "email"
    assert siteauth.classify_field("user", "password") == "password"      # type beats name
    assert siteauth.classify_field("confirm_password", "text") == "password"
    assert siteauth.classify_field("company_name", "text") == "company"   # 'company' before 'name'
    assert siteauth.classify_field("lastName", "text") == "last"
    assert siteauth.classify_field("mobileNumber", "text") == "phone"


def t_a_username_box_means_email_on_login_and_name_on_signup() -> None:
    """The one genuinely ambiguous field, and it is not hypothetical. tender18.com's registration
    form is `<input type=text name=username placeholder="Name">` sitting next to a real
    `type=email` box — classifying it as the login identifier types an email address into the
    Name field and the site rejects the form. Resolved per FORM: an unmistakable email box
    elsewhere means this one is a name."""
    assert siteauth.classify_field("username username Name", "text",
                                   email_elsewhere=True) == "name"
    assert siteauth.classify_field("username", "text", email_elsewhere=False) == "email"
    # ...and "username" must not fall through to the generic 'name' rule by substring.
    assert siteauth.classify_field("txtUserId", "text", email_elsewhere=False) == "email"


def t_a_soft_404_is_not_mistaken_for_a_registration_page() -> None:
    """tenderdetail.com answers /register, /signup, /login and every other unknown path with
    HTTP 200 by redirecting to /Country-tenders. A status check therefore "finds" five signup
    pages that do not exist, and the flow fills the site search box and calls it a registration.
    Measured live 2026-07-25."""
    assert siteauth.landed_on_guess("https://x.com/register", "https://x.com/register")
    assert siteauth.landed_on_guess("https://x.com/register/", "https://x.com/register")
    assert not siteauth.landed_on_guess("https://www.tenderdetail.com/register",
                                        "https://www.tenderdetail.com/Country-tenders")
    assert not siteauth.landed_on_guess("https://x.com/login", "https://x.com/")


def t_manual_credentials_skip_registration_but_keep_the_session_machinery() -> None:
    """The path that actually works on these portals: they issue logins through sales, so there
    is nothing to register. Seeding credentials must NOT look like a live session on its own —
    only a completed login writes cookies."""
    import json
    import tempfile

    d = Path(tempfile.mkdtemp())
    old_path, old_cache = siteauth.STORE_PATH, siteauth._CACHE
    try:
        siteauth.STORE_PATH = d / "site_auth.json"
        siteauth._CACHE = (-1.0, {})
        siteauth.set_credentials("https://www.tender18.com/", "me@corp.com", "s3cret")
        rec = siteauth.get_session("tender18.com")
        assert rec["email"] == "me@corp.com" and rec["manual"] is True
        assert not siteauth.is_fresh(rec), "credentials alone are not a session"
        assert siteauth.cookie_header("tender18.com") == ""
        assert "s3cret" not in json.dumps(siteauth.all_sessions()), "password must not be exposed"
    finally:
        siteauth.STORE_PATH, siteauth._CACHE = old_path, old_cache


def t_an_otp_or_captcha_box_is_never_guessed() -> None:
    """Filling these with junk turns "we could not register" into "we registered" plus a session
    that isn't one. Leaving them empty makes the submit fail where it can be seen."""
    for name in ("otp", "enter_otp", "captcha", "verification_code", "g-recaptcha-response"):
        assert siteauth.classify_field(name, "text") is None, name


def t_a_stale_or_empty_session_is_never_replayed(tmp_store=None) -> None:
    now = time.time()
    assert siteauth.is_fresh({"cookies": [{"name": "a", "value": "b"}], "saved_at": now})
    assert not siteauth.is_fresh({"cookies": [{"name": "a", "value": "b"}], "saved_at": 1.0})
    assert not siteauth.is_fresh({"cookies": [], "saved_at": now})
    assert not siteauth.is_fresh(None)


def t_the_cookie_header_is_host_scoped() -> None:
    """One portal's session must never be sent to another host. The store is keyed by host, so
    this is really a test that lookup can't fall through to "any session"."""
    import json
    import tempfile

    d = Path(tempfile.mkdtemp())
    old_path, old_cache = siteauth.STORE_PATH, siteauth._CACHE
    try:
        siteauth.STORE_PATH = d / "site_auth.json"
        siteauth._CACHE = (-1.0, {})
        siteauth.STORE_PATH.write_text(json.dumps({
            "tender18.com": {"cookies": [{"name": "sid", "value": "S1"}], "saved_at": time.time()},
        }), "utf-8")
        assert siteauth.cookie_header("https://www.tender18.com/x") == "sid=S1"
        assert siteauth.cookie_header("https://bidassist.com/x") == ""
        assert siteauth.all_sessions()[0]["cookies"] == 1
        assert "value" not in json.dumps(siteauth.all_sessions()), "cookie values must not leak"
    finally:
        siteauth.STORE_PATH, siteauth._CACHE = old_path, old_cache


def t_the_confirmation_link_is_picked_out_of_a_real_mail() -> None:
    body = ('<p>Welcome!</p><a href="https://tender18.com/unsubscribe?x=1">unsubscribe</a>'
            '<a href="https://tender18.com/verify?token=abc123">Confirm your email</a>')
    assert tempmail.extract_link(body) == "https://tender18.com/verify?token=abc123"


def t_a_newsletter_is_not_clicked_on_speculatively() -> None:
    """A mail with several URLs and no hint word is not a confirmation mail. Guessing would open
    something unrelated on the portal while logged in."""
    body = ('<a href="https://x.com/a">a</a><a href="https://x.com/b">b</a>'
            '<a href="https://x.com/c">c</a>')
    assert tempmail.extract_link(body) is None
    # ...but a single bare tokenised link IS the confirmation, which several portals send.
    assert tempmail.extract_link('Click https://x.com/u/9f2a to finish.') == "https://x.com/u/9f2a"


def t_a_generated_password_satisfies_the_usual_form_rules() -> None:
    for _ in range(20):
        p = tempmail.random_password()
        assert len(p) == 16 and any(c.isdigit() for c in p) and any(c.isupper() for c in p) \
            and any(c.islower() for c in p) and any(c in "!@#$%&*" for c in p)


def t_mailhog_refuses_to_pretend_it_can_receive_mail() -> None:
    """A container only receives a portal's mail if a domain's MX points at it. Handing back an
    address that can never receive anything would look exactly like "the site never sent it"."""
    import os
    old = dict(os.environ)
    try:
        os.environ["CRAWLER_TEMPMAIL_PROVIDER"] = "mailhog"
        os.environ.pop("CRAWLER_TEMPMAIL_DOMAIN", None)
        try:
            tempmail.new_inbox()
            raise AssertionError("mailhog provider accepted a missing CRAWLER_TEMPMAIL_DOMAIN")
        except tempmail.TempMailError as exc:
            assert "MX" in str(exc)
    finally:
        os.environ.clear()
        os.environ.update(old)


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} TENDER-DEFENCE TESTS PASSED")


def test_tender_defense_suite() -> None:
    main()


if __name__ == "__main__":
    main()
