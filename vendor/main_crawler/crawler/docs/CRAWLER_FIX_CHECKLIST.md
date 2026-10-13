# Crawler Fix Checklist

Tracks the extraction-quality fix effort scoped from the user's C1-C11 review of
`ddpmod.gov.in`-style tender pages. Research confirmed which claims were real bugs
vs. already-solved; only real gaps are in scope. Check items off as completed.

## Research (done)

- [x] Verify entity/name matching implementation (resolver.py) — confirmed O(N×L)
      sequential regex scans, no trie; "menu junk" bug is real but conditional
      (only on JS-interaction-rendered pages / trafilatura-failure fallback)
- [x] Verify PDF/tender extraction (pdfextract.py, extract.py, models.py) —
      confirmed `requirement_fields` is NEVER populated for PDF tenders (0%
      coverage); confirmed calibre/range/weight parsing doesn't exist at all
- [x] Verify crawl budgets, dedup, and link-bounding claims — confirmed
      per-job-type budgets (jobgen.py), dedup/skip-unchanged (dedup.py), and
      same-site/depth bounding (harvest.py) **already exist correctly**, not
      broken; confirmed link-text relevance filtering does NOT exist (genuinely
      new) and no ddpmod.gov.in golden fixture ever existed (nothing to restore)

## Scope decisions (confirmed with user)

- [x] Prioritize the 2 real extraction gaps (PDF/tender + entity matching) over
      re-implementing things that already work
- [x] Leave translation as fixture-only — no real MT provider wired this round

## Implementation — PDF/tender extraction (the biggest fix)

- [x] Add `pdfplumber` to requirements.txt (table-aware PDF extraction)
- [x] Extend `pdfextract.py` to extract tables alongside plain text
- [x] Add calibre/range/weight/military-quantity parser in `extractutil.py`
      (without breaking `find_money`'s deliberate calibre exclusion)
- [x] Wire `extract.py`'s `_requirement_fields()`/PDF branch to actually
      populate structured spec rows from PDF tables/text
- [x] Reconcile the two truncation points (pdfextract 200k cap vs.
      `req_text[:8000]` in extract.py) into one documented decision

## Implementation — Entity matching

- [x] Swap resolver.py's per-alias regex scan for a flashtext-based matcher
      (verify it preserves first-appearance ordering + unknown-company
      detection — required by existing tests). Found and fixed a real bug
      the plan hadn't anticipated: flashtext silently overwrites a keyword
      registered under more than one type/id (e.g. "Nagastra" is both a
      product alias and a UAV tech_domain example keyword) — fixed by
      indexing all registrations per alias string and fanning each trie hit
      back out to every type/id it represents.
- [x] Add targeted main-text-only fix for the two real bypass cases
      (interaction-rendered `inner_text`, trafilatura-failure fallback) —
      not a rewrite of the working common case
- [x] Add additive nearby-clue-word confidence boost (won/awarded/signed/etc.)

## Implementation — Link-text relevance (new, opt-in)

- [x] Add opt-in Job-level flag for anchor-text relevance filtering
- [x] Wire into `extract_links()`/harvest.py call site without changing
      default behavior for existing jobs

## Verification

- [x] Unit tests for new spec/quantity parser (calibre/range/weight cases)
- [x] Unit tests for flashtext-based resolver (must pass existing 3 resolver
      tests unchanged: multi-type resolution, unknown-company flagging,
      first-appearance ordering)
- [x] Run against `tests/fixtures/mod_rfp.pdf` + `mod_tender.html` and confirm
      `requirement_fields` is now non-empty with plausible values
- [x] Full regression: `pytest tests/` (excluding test_ollama_api.py) — 58 passed
- [x] Offline batch: `python run.py testing` — all 7 exit criteria still pass
