"""Site-classification probe — recommend Job params from ONE cheap fetch.

The operator uploads a CSV of defence sites; before jobgen we must guess two Job knobs per URL:

  * ``keyword`` — the per-page relevance gate. False for PURE-defence sites (capture everything),
    True for MIXED sites (a defence SECTION of a general publisher, where the crawl escapes into
    sport/entertainment).
  * ``render_js`` — true only when the httpx body is a JS shell.

Three things this probe deliberately does NOT do, each measured against jobs_india_defence.json
(66 labelled URLs, 55 pure / 11 mixed):

  1. It does not gate on the keyword corpus (``keywords.py``, 2259 surfaces). That corpus is
     overwhelmingly NAMED ENTITIES (companies/products), not topic vocabulary — mod.gov.in,
     drdo.gov.in, sipri.org and thalesgroup.com all score 0.0 hits/1000 words yet are pure
     defence. Corpus density is carried as an informational measurement only.
  2. It does not use ``parse.visible_text`` for identity. That helper decomposes nav/header/footer
     (parse.py:47-49), which is right for article extraction and wrong for a SITE probe — the nav
     is exactly where site identity lives ("Missile Systems / Naval" vs "Sports / Bollywood /
     Horoscope"). ``_identity_surface`` keeps the chrome, plus <title>, meta description/og and
     link path slugs.
  3. It does not pretend a threshold is an oracle. One fetch caps accuracy near ~86% against an
     82.8% majority baseline, so every result carries a confidence band and ``review_required``.

The real discriminator is link-graph SECTION BREADTH: histogram the first path segment of the
same-host links and count how many distinct sections are off-topic.

No robots, no delay, no retries, no PDF, no Playwright, no dedup, no ingest — one GET.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import parse

# Editorial sections that a PURE defence site has no reason to publish. Presence of several
# distinct ones in the link graph is the mixed-publisher signature.
OFFTOPIC_SECTIONS: frozenset[str] = frozenset({
    "sport", "sports", "cricket", "football", "hockey", "tennis", "olympics", "ipl",
    "entertainment", "bollywood", "hollywood", "movies", "movie", "film", "films", "tv",
    "music", "celebrity", "celebrities", "gossip", "photos", "gallery", "videos",
    "horoscope", "astrology", "zodiac", "religion", "spirituality",
    "lifestyle", "life-style", "fashion", "beauty", "food", "recipes", "travel",
    "health", "fitness", "wellness", "education", "exam", "exams", "results",
    "jobs", "career", "careers", "realestate", "real-estate", "property",
    "auto", "autos", "cars", "bike", "gadgets", "mobiles", "shopping", "deals",
    "astro", "weather", "opinion", "blogs", "podcast", "games", "gaming",
})

# The identity-TEXT signal needs a much narrower list than the link histogram, because the two
# surfaces mean different things. A URL section literally named /food/ or /education/ is a
# publishing vertical (real breadth evidence); the same WORDS in nav/meta prose are usually
# subject matter or universal chrome. Measured: sipri.org hit education/film/films/food from
# "peace education" and "food, peace and security", and mod.gov.in hit gallery/videos from a photo
# gallery — both scored >=3 and false-positived as mixed with ZERO off-topic sections. So the
# text rule keeps only tokens a defence institution would never put in its own nav.
_IDENTITY_OFFTOPIC: frozenset[str] = frozenset({
    "sport", "sports", "cricket", "football", "hockey", "tennis", "ipl",
    "entertainment", "bollywood", "hollywood", "movies", "celebrity", "celebrities", "gossip",
    "horoscope", "astrology", "zodiac", "astro", "lifestyle", "life-style", "fashion", "beauty",
    "recipes", "travel", "fitness", "wellness", "realestate", "real-estate", "property",
    "autos", "cars", "bike", "gadgets", "mobiles", "shopping", "deals", "gaming",
})

# TOPIC vocabulary (NOT entities) — words a defence site uses about its subject matter. Used only
# for the identity surface, never as a density gate (see module docstring, point 1).
DEFENCE_TOPIC_TERMS: frozenset[str] = frozenset({
    "defence", "defense", "military", "army", "navy", "naval", "air force", "airforce",
    "soldier", "troops", "regiment", "battalion", "brigade", "infantry", "cavalry",
    "missile", "rocket", "artillery", "ammunition", "munitions", "ordnance", "warhead",
    "radar", "sonar", "avionics", "aerospace", "airborne", "fighter jet", "aircraft",
    "helicopter", "drone", "uav", "submarine", "warship", "frigate", "destroyer",
    "corvette", "carrier", "shipyard", "armoured", "armored", "tank", "howitzer",
    "procurement", "tender", "rfp", "contract award", "acquisition", "indigenisation",
    "indigenization", "arms", "weapon", "weapons", "firearms", "surveillance",
    "reconnaissance", "intelligence", "security", "strategic", "deterrence", "nato",
    "veteran", "combat", "warfare", "cyber defence", "border", "paramilitary",
    "ministry of defence", "armed forces", "national security", "arms export",
})

_WORD = re.compile(r"[a-z][a-z\-']+")
_LANG = re.compile(r"<html[^>]*\blang\s*=\s*[\"']?([a-zA-Z\-]+)", re.I)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Path segments that carry no section meaning — dates, ids, language prefixes, generic wrappers.
_SECTION_NOISE = frozenset({"en", "hi", "in", "us", "uk", "www", "index", "home", "page",
                            "story", "article", "articles", "news", "content", "amp"})


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().lstrip("www.")
    except ValueError:
        return ""


def _section_of(url: str) -> str | None:
    """First meaningful path segment, lowercased — the site's own section label."""
    try:
        segs = [s for s in urlsplit(url).path.split("/") if s]
    except ValueError:
        return None
    for s in segs[:2]:                       # look past a /en/ or /news/ wrapper, no further
        tok = s.lower().split(".")[0].strip()
        if not tok or tok.isdigit() or len(tok) > 40 or tok in _SECTION_NOISE:
            continue
        return tok
    return None


def _identity_surface(html: str, base_url: str) -> tuple[str, list[str]]:
    """Site-identity text + same-host links.

    KEEPS nav/header/footer (unlike parse.visible_text) and folds in <title>, meta
    description/og:*, and the slugs of every same-host link — a site's menu and URL vocabulary
    identify it far better than its front-page article text does.
    """
    soup = parse._soup(html)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()

    bits: list[str] = []
    if soup.title and soup.title.get_text(strip=True):
        bits.append(soup.title.get_text(strip=True))
    for m in soup.find_all("meta"):
        key = (m.get("name") or m.get("property") or "").lower()
        if key in ("description", "keywords", "og:title", "og:description", "og:site_name"):
            bits.append(m.get("content") or "")
    # Chrome first: nav/header/footer text is the site's own table of contents.
    for tag in soup.find_all(["nav", "header", "footer"]):
        bits.append(tag.get_text(" ", strip=True))

    host = _host(base_url)
    try:
        links = parse.extract_links(html, base_url)
    except Exception:
        links = []
    same_host = [u for u in links if _host(u) == host]
    # Slugs are identity too: /missile-systems/ vs /bollywood/ says more than any body text.
    bits.extend(u.replace("/", " ").replace("-", " ").replace("_", " ") for u in same_host[:400])

    return re.sub(r"\s+", " ", " ".join(b for b in bits if b)).strip().lower(), same_host


def _section_breadth(same_host_links: list[str]) -> dict:
    """Histogram the first path segment of the same-host links and count off-topic sections.

    This is THE discriminator: a mixed site is a defence section bolted onto a general publisher,
    so its link graph fans out into sport/bollywood/horoscope. A pure site's sections are all
    subject matter.
    """
    sections: dict[str, int] = {}
    for u in same_host_links:
        sec = _section_of(u)
        if sec:
            sections[sec] = sections.get(sec, 0) + 1
    offtopic = {s: n for s, n in sections.items() if s in OFFTOPIC_SECTIONS}
    total = sum(sections.values())
    return {
        "distinct_sections": len(sections),
        "distinct_offtopic_sections": len(offtopic),
        "offtopic_link_ratio": round(sum(offtopic.values()) / total, 4) if total else 0.0,
        "top_offtopic": sorted(offtopic, key=lambda s: -offtopic[s])[:5],
    }


def _count_terms(terms: frozenset[str], tokens: set[str], surface: str) -> int:
    """Distinct terms present, matched on WORD BOUNDARIES. Substring matching is not merely
    sloppy here, it inverts the answer: "sport" occurs inside "transport", so SIPRI's arms-transfer
    vocabulary scored 4 off-topic identity terms and the site classified mixed. Single-word terms
    hit the token set; multiword phrases fall back to substring on the space-normalised surface,
    where the phrase itself supplies the boundaries.
    """
    n = 0
    for t in terms:
        norm = t.replace("-", " ")
        n += (f" {norm} " in surface) if " " in norm else (norm in tokens)
    return n


def _seed_path_prefix(url: str) -> str:
    """The seed's section prefix, e.g. https://x.com/topic/defence/ -> "/topic/defence"."""
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        return ""
    segs = [s for s in path.split("/") if s]
    if segs and "." in segs[-1]:
        segs = segs[:-1]                     # /topic/defence/index.html -> /topic/defence
    return ("/" + "/".join(segs)) if segs else ""


def recommend(m: dict) -> dict:
    """PURE measurements -> recommendation. No I/O; the tests drive this directly.

    Gate rule: default keyword=False (pure — capture everything), flipped to True ONLY on
    positive breadth evidence. The default is asymmetric on purpose: `keyword:true` on a pure
    site SILENTLY DROPS real pages that nobody ever sees missing, whereas `keyword:false` on a
    mixed site ingests visible, correctable noise. Prefer the recoverable error.
    """
    ratio = float(m.get("offtopic_link_ratio") or 0.0)
    links = int(m.get("same_host_links") or 0)
    off_secs = int(m.get("distinct_offtopic_sections") or 0)
    id_terms = int(m.get("identity_offtopic_terms") or 0)
    words = int(m.get("body_words") or 0)

    mixed = (ratio >= 0.25 and links >= 15) or off_secs >= 4 or id_terms >= 3

    # Confidence. `needs_render` is coupled to the density signals, not independent of them: a JS
    # shell has no text, so body_words/link counts measure the shell, not the site — the whole
    # measurement is meaningless and must be reviewed regardless of which way the gate fell.
    lang = (m.get("html_lang") or "").lower()
    fetch_ok = bool(m.get("fetch_ok"))
    low = (not fetch_ok or words < 150 or links < 10 or bool(m.get("needs_render"))
           or (lang and not lang.startswith("en")))
    if low:
        confidence = "low"
    elif words >= 300 and links >= 20:
        confidence = "high"
    else:
        confidence = "medium"

    # path_scope beats the gate flip when the seed already names a section: restricting to
    # "/topic/defence" keeps every page IN that section (no silent drops) while shutting out the
    # publisher's related-article graph. See the path_scope docstring in models.py (~line 89-93).
    prefix = m.get("seed_path_prefix") or ""
    path_scope = [prefix] if (prefix and mixed) else []

    # When path_scope applies it SUPPRESSES the gate flip. Confining the crawl to /topic/defence
    # already excludes the sport/entertainment graph, so everything left IS defence — and leaving
    # keyword=true on top would re-introduce the exact failure the gate causes on a pure site:
    # silently dropping in-section pages that happen to contain no corpus entity. Belt, not both.
    gate = mixed and not path_scope

    return {"keyword": gate, "render_js": bool(m.get("needs_render")),
            "path_scope": path_scope, "confidence": confidence,
            "review_required": confidence == "low"}


def _evidence(m: dict, rec: dict) -> str:
    """One line the operator can act on without opening the site."""
    if not m.get("fetch_ok"):
        return f"fetch failed ({m.get('error') or m.get('status')}) -> no signal (low)"
    secs = m.get("distinct_sections") or 0
    off = m.get("distinct_offtopic_sections") or 0
    names = ", ".join(m.get("top_offtopic") or []) or "none"
    # A path_scope site IS mixed — the gate is off only because the scope already excludes the
    # off-topic graph. Saying "pure" here would misread as "no breadth detected".
    verdict = "mixed" if (rec["keyword"] or rec["path_scope"]) else "pure"
    line = (f"{off} of {secs} sections off-topic ({names}), "
            f"{int(round(100 * float(m.get('offtopic_link_ratio') or 0)))}% of "
            f"{m.get('same_host_links') or 0} links -> {verdict} ({rec['confidence']})")
    if rec["path_scope"]:
        line += (f"; confined to path_scope {rec['path_scope']} instead of the keyword gate "
                 f"(scope keeps every in-section page; the gate would silently drop some)")
    if m.get("needs_render"):
        line += "; JS shell — text measurement unreliable"
    return line


def probe_site(url: str, *, corpus_kp=None, timeout_s: float = 8.0,
               user_agent: str | None = None) -> dict:
    """One GET -> {url, final_url, status, error, recommend, evidence, measurements}. Never raises.

    Uses the pooled shared httpx client — a probe sweep of a CSV is hundreds of hosts, and a fresh
    client per URL would pay a TLS handshake each time.
    """
    from .fetcher import FetchResult, _classify_kind, _shared_http_client

    m: dict = {"fetch_ok": False, "body_words": 0, "same_host_links": 0, "distinct_sections": 0,
               "distinct_offtopic_sections": 0, "offtopic_link_ratio": 0.0, "top_offtopic": [],
               "identity_offtopic_terms": 0, "identity_defence_terms": 0,
               "corpus_hits_per_1k": 0.0, "needs_render": False, "html_lang": None,
               "seed_path_prefix": _seed_path_prefix(url), "status": None, "error": None}
    final_url, status, error = url, None, None

    try:
        client = _shared_http_client(True)
        resp = client.get(url, timeout=timeout_s,
                          headers={"User-Agent": user_agent or _UA,
                                   "Accept-Language": "en-US,en;q=0.9"})
        status = resp.status_code
        final_url = str(resp.url)
        ct = resp.headers.get("content-type")
        html = resp.text if "text" in (ct or "").lower() or "html" in (ct or "").lower() else ""
        fr = FetchResult(url=url, final_url=final_url, status=status, content_type=ct,
                         kind=_classify_kind(ct, final_url), text_html=html or None)

        # _needs_render is the production JS-shell heuristic (async_engine.py:197) — reused
        # verbatim so the probe's render_js recommendation matches what the engine will actually do.
        from .async_engine import _needs_render
        m["needs_render"] = _needs_render(fr)

        if html and status and status < 400:
            m["fetch_ok"] = True
            surface, same_host = _identity_surface(html, final_url)
            m.update(_section_breadth(same_host))
            m["same_host_links"] = len(same_host)
            lang = _LANG.search(html)
            m["html_lang"] = lang.group(1).lower() if lang else None

            body = parse.visible_text(html)
            words = _WORD.findall(body.lower())
            m["body_words"] = len(words)
            surf_norm = " " + surface.replace("-", " ") + " "
            surf_tokens = set(_WORD.findall(surface.replace("-", " ")))
            m["identity_offtopic_terms"] = _count_terms(_IDENTITY_OFFTOPIC, surf_tokens, surf_norm)
            m["identity_defence_terms"] = _count_terms(DEFENCE_TOPIC_TERMS, surf_tokens, surf_norm)
            if corpus_kp is not None and words:
                try:
                    hits = len(set(corpus_kp.extract_keywords(body)))
                    m["corpus_hits_per_1k"] = round(1000 * hits / len(words), 3)
                except Exception:
                    pass                      # informational only — never fail the probe on it
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]

    m["status"], m["error"] = status, error
    rec = recommend(m)
    return {"url": url, "final_url": final_url, "status": status, "error": error,
            "recommend": rec, "evidence": _evidence(m, rec), "measurements": m}
