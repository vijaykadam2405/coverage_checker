"""Coverage checks - is the crawler actually crawling the catalogue PROPERLY?

`services` proves the stack is UP and `tiers` proves the fetchers WORK; this area proves the
thing those enable is actually happening: that pages are landing, that sites go past their seed,
that depth accumulates, that the keep-gate is keeping (not condemning everything), and that what
lands is real text/html rather than empty shells. A crawler can be green on every liveness probe
and still be doing nothing useful - a stalled frontier, a gate dropping 100%, or a fleet that
only ever fetches seed pages all look "healthy" until you count what reached the corpus.

The failure classes these checks are written to catch (each has bitten this deployment before -
see the memory notes of the same names):
  * STALLED FRESHNESS - the crawl is alive on paper but no new document has been ingested in the
    window ("what complete means", "batch stuck at stopping"). freshness catches it.
  * ONE-PAGE SITES / NAV-PERMUTATION - a site holds only its seed, or one source's menu permuting
    itself dominates the whole corpus ("one page is not a crawl", "nav-permutation trap",
    "generator traps"). one-page-sites, beyond-seed and per-site-distribution catch these.
  * ZERO-PAGE SOURCES - a domain is enrolled in the catalogue but nothing was ever stored for it
    ("apex DNS dead www alive", "seed probes must recur", "the scheduler saw one instance").
    zero-page-enrolled catches it.
  * EMPTY SHELLS - pages are counted but carry no extracted text (WAF interstitials, JS shells,
    stored 403s: "error pages stored as content"). text-capture catches it via text_len.
  * A CONDEMNING GATE - an English-only or mis-scoped keep-gate drops every page ("multilingual
    defence-tender harvest", "keep-gate uses the global corpus"). gate-sanity catches kept==0.

DESIGN RULES (inherited from hc.py, do not break):
  * READ-ONLY. No writes, no dispatch. Only cheap Postgres reads and the ingest/orchestrator
    read endpoints the dashboards already poll.
  * NEVER select main_text/html - those are the TOASTed body columns and summing/length-ing them
    over the corpus is a known outage. Every query here uses text_len / html_len / coalesce, the
    scalar twins pgstore stores for exactly this reason. hc.pg() also refuses a body-column query.
  * ingested_at is TEXT (ISO-8601) and is compared AS TEXT against a pre-formatted bound so the
    documents_ingested_at index is usable - casting it per row is a full-corpus scan (measured at
    16 minutes live). The recent-window bound is built in Python, mirroring pgstore.live_summary.
  * Bounded: the heavy work is a single index-served aggregate over the recent window, memoised
    for the process so the depth/text/html/pdf checks share one scan.

Thresholds are env-driven (HC_COVERAGE_*) so they tune without a code change, and every check
states the numbers it observed in its detail string.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import hc

# -- tunable thresholds (generous defaults - these fail on pathology, not on a quiet day) -------
# Fraction of crawled sources that may hold exactly one page before it is a fault.
ONEPAGE_MAX_FRAC = float(hc.cfg("HC_COVERAGE_ONEPAGE_MAX_FRAC", "0.6"))
# Fraction of ENROLLED catalogue sources that may have zero stored pages before it is a fault.
ZEROPAGE_MAX_FRAC = float(hc.cfg("HC_COVERAGE_ZEROPAGE_MAX_FRAC", "0.5"))
# Fraction of recent documents that may be empty (text_len == 0) before it is a fault.
EMPTY_TEXT_MAX_FRAC = float(hc.cfg("HC_COVERAGE_EMPTY_TEXT_MAX_FRAC", "0.4"))
# Fraction of the whole corpus a SINGLE source may hold before it reads as pathological domination.
TOP_SOURCE_MAX_FRAC = float(hc.cfg("HC_COVERAGE_TOP_SOURCE_MAX_FRAC", "0.9"))
# Minimum gate decisions (kept + dropped) before "kept == 0" is trusted as a real condemning-gate.
GATE_MIN_DECISIONS = int(float(hc.cfg("HC_COVERAGE_GATE_MIN_DECISIONS", "20")))


# -- shared, memoised recent-window snapshot (ONE index-served scan for several checks) ---------
_SNAP: dict = {}


def _recent_bound() -> str:
    """The lower bound for "recent", formatted exactly like the stored ingested_at values (no 'Z',
    to the second) so a lexicographic `ingested_at > bound` is a correct - and index-usable -
    time comparison. Mirrors pgstore.live_summary's to_char() bound."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hc.RECENT_HOURS)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _recent_snapshot() -> dict:
    """count/text/html/depth split of the last RECENT_HOURS, in one aggregate. Scalar columns only
    (text_len/html_len/depth) so it never touches TOAST. Memoised for the process - the depth,
    text-capture, html-capture and freshness checks all read it. Propagates hc.Skip (no DB)."""
    if "row" in _SNAP:
        return _SNAP["row"]
    bound = _recent_bound()
    rows = hc.pg(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE coalesce(text_len,0) > 0) AS with_text, "
        "count(*) FILTER (WHERE coalesce(html_len,0) > 0) AS with_html, "
        "count(*) FILTER (WHERE depth > 0) AS beyond_seed, "
        "count(*) FILTER (WHERE depth = 0) AS at_seed, "
        "count(*) FILTER (WHERE depth IS NULL) AS depth_null "
        "FROM documents WHERE ingested_at > %s",
        (bound,))
    row = dict(rows[0]) if rows else {"total": 0, "with_text": 0, "with_html": 0,
                                      "beyond_seed": 0, "at_seed": 0, "depth_null": 0}
    row["bound"] = bound
    _SNAP["row"] = row
    return row


def _summary() -> dict:
    """The ingest live summary (cached server-side; TOAST-safe)."""
    return hc.http_json(f"{hc.INGEST_BASE}/api/live/summary")


def _sources_list() -> list[dict]:
    """Every crawled source with its page count (pgstore.by_source, via the dashboard endpoint).
    Sorted pages-desc by the endpoint. Empty list when the store is unreachable/empty."""
    got = hc.http_json(f"{hc.INGEST_BASE}/api/live/sources?limit=100000")
    return got if isinstance(got, list) else []


def _norm_domain(d: str) -> str:
    d = (d or "").strip().lower()
    return d[4:] if d.startswith("www.") else d


# -- freshness: is the crawl actually adding pages right now? ------------------------------------
@hc.check(area="coverage", name="freshness: recent ingest", critical=False)
def freshness():
    """FAIL if zero documents were ingested in the last RECENT_HOURS - a live crawl must land pages.
    Uses the recent-window snapshot; falls back to the summary's newest_at when there is no DB."""
    try:
        snap = _recent_snapshot()
    except hc.Skip:
        # No local DB - still answer the question from the cached summary's max(ingested_at).
        pg = (_summary().get("postgres") or {})
        if pg.get("error"):
            raise hc.Skip(f"store not readable: {pg['error']}")
        newest = pg.get("newest_at")
        if not newest:
            raise AssertionError("no documents in the corpus at all (newest_at is null)")
        try:
            ts = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            raise hc.Skip(f"could not parse newest_at={newest!r}")
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        hc.need(age_h <= hc.RECENT_HOURS,
                f"newest document is {age_h:.1f}h old (> {hc.RECENT_HOURS:.0f}h window) - crawl stalled")
        return f"newest ingest {age_h:.1f}h ago (window {hc.RECENT_HOURS:.0f}h; via summary)"
    n = snap["total"]
    hc.need(n > 0, f"0 documents ingested in the last {hc.RECENT_HOURS:.0f}h - crawl appears stalled")
    return f"{n:,} documents ingested in the last {hc.RECENT_HOURS:.0f}h"


# -- volume: how big is the corpus (informational) --------------------------------------------
@hc.check(area="coverage", name="volume: corpus size", critical=False)
def volume():
    """Report total documents and distinct sources. Informational - a trend line, not a gate. Skips
    (rather than fails) when the store is unreachable, since `services` owns store liveness."""
    pg = (_summary().get("postgres") or {})
    if pg.get("error"):
        raise hc.Skip(f"store not readable: {pg['error']}")
    docs = pg.get("documents")
    if docs is None:
        raise hc.Skip("summary returned no document count")
    return (f"corpus holds {int(docs):,} documents across {int(pg.get('sources') or 0):,} sources "
            f"({int(pg.get('text_chars') or 0):,} text chars stored)")


# -- beyond-seed: does the crawl follow links, or only fetch seeds? ---------------------------
@hc.check(area="coverage", name="beyond-seed: depth accumulates", critical=False)
def beyond_seed():
    """A crawl that follows links produces pages at depth > 0; a fleet that only fetches seeds does
    not. FAIL when the recent window has documents whose depth is tracked yet NONE go past the seed.
    Skips when depth isn't tracked on recent captures (some feed/API fallbacks don't stamp it), and
    falls back to the per-source depth endpoint when there is no DB."""
    try:
        snap = _recent_snapshot()
    except hc.Skip:
        # No DB - sample the busiest crawled source and read its depth breakdown over HTTP.
        srcs = _sources_list()
        if not srcs:
            raise hc.Skip("no DB and no crawled sources to sample depth from")
        top = max(srcs, key=lambda s: s.get("pages") or 0)
        dom = top.get("source_id") or ""
        depths = (hc.http_json(f"{hc.INGEST_BASE}/api/live/source/{dom}/depth").get("depths") or [])
        beyond = sum(int(d.get("pages") or 0) for d in depths if (d.get("depth") or 0) > 0)
        hc.need(beyond > 0,
                f"busiest source {dom!r} has {top.get('pages')} pages but none beyond the seed")
        return f"{dom!r}: {beyond:,} pages beyond the seed (sampled, no DB)"
    total = snap["total"]
    if total == 0:
        raise hc.Skip("no recent documents to assess depth (see freshness)")
    beyond, at_seed, unknown = snap["beyond_seed"], snap["at_seed"], snap["depth_null"]
    if beyond == 0 and unknown == total:
        raise hc.Skip(f"depth not tracked on any of the {total:,} recent captures")
    hc.need(beyond > 0,
            f"of {total:,} recent docs, {at_seed:,} are seeds and NONE go deeper "
            f"({unknown:,} untracked) - the crawl is not following links")
    return (f"{beyond:,} of {total:,} recent docs are beyond the seed "
            f"(seeds {at_seed:,}, depth-untracked {unknown:,})")


# -- one-page sites: a stored seed is not a crawl ---------------------------------------------
@hc.check(area="coverage", name="one-page sites", critical=False)
def one_page_sites():
    """Count crawled sources holding exactly one page - the "one page is not a crawl" class. Always
    reports the count; FAILs only when the FRACTION of sources that are one-pagers exceeds
    HC_COVERAGE_ONEPAGE_MAX_FRAC (generous default), which is the shape of a fleet-wide stall."""
    srcs = [s for s in _sources_list() if (s.get("source_id") or "(none)") != "(none)"]
    if not srcs:
        raise hc.Skip("no crawled sources reported")
    onepage = [s for s in srcs if (s.get("pages") or 0) == 1]
    frac = len(onepage) / len(srcs)
    examples = ", ".join(s.get("source_id", "?") for s in onepage[:5])
    hc.need(frac <= ONEPAGE_MAX_FRAC,
            f"{len(onepage):,}/{len(srcs):,} sources ({frac:.0%}) hold exactly ONE page "
            f"(> {ONEPAGE_MAX_FRAC:.0%} threshold) - e.g. {examples}")
    return (f"{len(onepage):,}/{len(srcs):,} sources ({frac:.0%}) are one-pagers "
            f"(threshold {ONEPAGE_MAX_FRAC:.0%})"
            + (f"; e.g. {examples}" if examples else ""))


# -- zero-page enrolled sources: enrolled but nothing stored ----------------------------------
@hc.check(area="coverage", name="zero-page enrolled sources", critical=False)
def zero_page_enrolled():
    """Cross-reference the orchestrator catalogue (enrolled domains) against what actually has pages
    in the corpus. A domain enrolled but never stored is the "apex DNS dead / scheduler saw one
    instance / seed probe never recurred" class. FAILs when the fraction of enrolled sources with
    zero pages exceeds HC_COVERAGE_ZEROPAGE_MAX_FRAC. source_id (documents) and Source.domain
    (catalogue) are both lowercase registrable domains, so they join directly."""
    try:
        enrolled = hc.http_json(f"{hc.ORCH_BASE}/api/sources")
    except Exception as e:  # noqa: BLE001 - orchestrator not reachable is a skip, not a coverage fail
        raise hc.Skip(f"orchestrator /sources not readable: {str(e)[:120]}")
    if not isinstance(enrolled, list) or not enrolled:
        raise hc.Skip("no enrolled sources in the catalogue")
    enrolled_domains = {_norm_domain(s.get("domain", "")) for s in enrolled}
    enrolled_domains.discard("")
    crawled = {_norm_domain(s.get("source_id", "")) for s in _sources_list()
               if (s.get("pages") or 0) > 0}
    crawled.discard("")
    zero = sorted(enrolled_domains - crawled)
    frac = len(zero) / len(enrolled_domains) if enrolled_domains else 0.0
    examples = ", ".join(zero[:5])
    hc.need(frac <= ZEROPAGE_MAX_FRAC,
            f"{len(zero):,}/{len(enrolled_domains):,} enrolled sources ({frac:.0%}) have ZERO pages "
            f"(> {ZEROPAGE_MAX_FRAC:.0%} threshold) - e.g. {examples}")
    return (f"{len(zero):,}/{len(enrolled_domains):,} enrolled sources ({frac:.0%}) have no pages "
            f"(threshold {ZEROPAGE_MAX_FRAC:.0%})"
            + (f"; e.g. {examples}" if examples else ""))


# -- text capture: real text, not empty shells -----------------------------------------------
@hc.check(area="coverage", name="text capture (not empty shells)", critical=False)
def text_capture():
    """Fraction of recent documents carrying extracted text (text_len > 0). Empty shells - WAF
    interstitials, JS-only pages, stored 403s - have html but no text. FAILs when the EMPTY fraction
    exceeds HC_COVERAGE_EMPTY_TEXT_MAX_FRAC. Uses text_len only (never main_text)."""
    snap = _recent_snapshot()
    total = snap["total"]
    if total == 0:
        raise hc.Skip("no recent documents to assess text capture (see freshness)")
    with_text = snap["with_text"]
    empty_frac = 1.0 - (with_text / total)
    hc.need(empty_frac <= EMPTY_TEXT_MAX_FRAC,
            f"{total - with_text:,}/{total:,} recent docs ({empty_frac:.0%}) have NO text "
            f"(> {EMPTY_TEXT_MAX_FRAC:.0%} threshold) - likely empty shells")
    return (f"{with_text:,}/{total:,} recent docs have text ({1 - empty_frac:.0%}); "
            f"empty {empty_frac:.0%} (threshold {EMPTY_TEXT_MAX_FRAC:.0%})")


# -- html capture: informational --------------------------------------------------------------
@hc.check(area="coverage", name="html capture", critical=False)
def html_capture():
    """Fraction of recent documents with stored html (html_len > 0). Informational - a PDF-only or
    feed/API capture legitimately carries no html, so this reports rather than gates."""
    snap = _recent_snapshot()
    total = snap["total"]
    if total == 0:
        raise hc.Skip("no recent documents to assess html capture (see freshness)")
    with_html = snap["with_html"]
    return f"{with_html:,}/{total:,} recent docs have html ({with_html / total:.0%})"


# -- gate sanity: the keep-gate is keeping, not condemning everything -------------------------
@hc.check(area="coverage", name="gate sanity", critical=False)
def gate_sanity():
    """Read the CURRENT batch's crawl counters from the live summary (kept / dropped_by_gate / sent /
    accepted). The failure class is a gate that condemns EVERYTHING - an English-only or mis-scoped
    keep-gate that dropped every page. FAIL when there were enough gate decisions to trust but kept
    is zero. A high drop ratio alone is NOT a fault (keyword=off keeps all; require_keywords tender
    crawls legitimately drop most listing/pagination pages), so it is reported, not gated. Skips
    when no batch counters are present (no crawl has run since the crawler-api last restarted)."""
    batch = _summary().get("batch") or {}
    if not batch.get("reachable"):
        raise hc.Skip(f"crawler-api not reachable: {batch.get('error', 'unknown')}")
    totals = batch.get("totals") or {}
    kept, dropped = totals.get("kept"), totals.get("dropped_by_gate")
    sent, accepted = totals.get("sent"), totals.get("accepted")
    if kept is None and dropped is None:
        raise hc.Skip("no gate counters in the current batch (no crawl since restart)")
    kept, dropped = int(kept or 0), int(dropped or 0)
    decisions = kept + dropped
    if decisions < GATE_MIN_DECISIONS:
        raise hc.Skip(f"only {decisions} gate decision(s) so far - too few to judge (min "
                      f"{GATE_MIN_DECISIONS})")
    drop_frac = dropped / decisions
    hc.need(kept > 0,
            f"the keep-gate dropped ALL {decisions:,} pages (kept 0) - a condemning gate")
    return (f"kept {kept:,}, dropped {dropped:,} ({drop_frac:.0%} of {decisions:,} decisions), "
            f"sent {int(sent or 0):,}, accepted {int(accepted or 0):,}")


# -- per-site distribution: no single source dominating pathologically ------------------------
@hc.check(area="coverage", name="per-site distribution", critical=False)
def per_site_distribution():
    """Report the top sources by page count and the share the single biggest holds. A healthy corpus
    is spread across many sites; one source holding almost everything is the nav-permutation /
    generator-trap signature. Informational unless a single source exceeds HC_COVERAGE_TOP_SOURCE_
    MAX_FRAC of the WHOLE corpus (and there is more than one source)."""
    srcs = [s for s in _sources_list() if (s.get("source_id") or "(none)") != "(none)"]
    if not srcs:
        raise hc.Skip("no crawled sources reported")
    srcs.sort(key=lambda s: s.get("pages") or 0, reverse=True)
    total_pages = sum(int(s.get("pages") or 0) for s in srcs)
    if total_pages == 0:
        raise hc.Skip("crawled sources hold zero pages")
    top = srcs[0]
    top_frac = (top.get("pages") or 0) / total_pages
    lead = "; ".join(f"{s.get('source_id')}={int(s.get('pages') or 0):,}" for s in srcs[:5])
    if len(srcs) > 1:
        hc.need(top_frac <= TOP_SOURCE_MAX_FRAC,
                f"a single source {top.get('source_id')!r} holds {top_frac:.0%} of all "
                f"{total_pages:,} pages (> {TOP_SOURCE_MAX_FRAC:.0%}) - pathological domination")
    return (f"top of {len(srcs):,} sources: {lead} "
            f"(biggest = {top_frac:.0%} of {total_pages:,} pages)")


# -- pdf / attachment capture ------------------------------------------------------------------
@hc.check(area="coverage", name="pdf/attachment capture", critical=False)
def pdf_capture():
    """Confirm attachments (PDFs) are being captured. Prefers a recent-window count from the store;
    falls back to the corpus-wide count from /api/live/types when there is no DB. PASS on any
    captured; Skip (not FAIL) when none - a crawl window can legitimately hold no PDFs."""
    n = None
    try:
        # attachments is JSONB (not a TOAST body column); bounded to the recent window by the
        # ingested_at index, so this reads only the recent slice, not the whole corpus.
        n = int(hc.pg_scalar(
            "SELECT count(*) FROM documents WHERE ingested_at > %s "
            "AND attachments IS NOT NULL AND jsonb_array_length(attachments) > 0",
            (_recent_bound(),)) or 0)
    except Exception:  # noqa: BLE001 - hc.Skip (no DB/psycopg) included: fall back to the corpus tile
        n = None
    if n is not None:
        # A successful read: 0 attachments is a Skip (a window can hold none), not the fallback.
        if n == 0:
            raise hc.Skip(f"no attachments captured in the last {hc.RECENT_HOURS:.0f}h "
                          f"(not necessarily a fault)")
        return f"{n:,} documents with attachments ingested in the last {hc.RECENT_HOURS:.0f}h"
    types = (hc.http_json(f"{hc.INGEST_BASE}/api/live/types").get("types") or {})
    if types.get("error"):
        raise hc.Skip(f"types not readable: {types['error']}")
    pdfs = int(types.get("pdf_attachments") or 0)
    if pdfs == 0:
        raise hc.Skip("no PDF attachments stored in the corpus (not necessarily a fault)")
    return f"{pdfs:,} PDF attachments stored corpus-wide (no DB; not windowed)"
