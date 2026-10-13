"""Report keyed on the ORIGINAL source spreadsheet, not on what survived.

Every earlier report started from the live catalogue, which silently answers a different question:
it can only describe sources that are still there. A row dropped during the project vanishes from
the denominator entirely, so "592 sources, 215 fully crawled" hides the 247 that were removed.
This starts from `final urls (2).xlsx` — every manufacturer row as originally selected — and says
what became of each one.

    python build_original_report.py original_manufacturers.json manufacturers.json \
           persite.json corpuspages.json out.xlsx
"""
from __future__ import annotations

import io
import json
import sys
from collections import Counter

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HDR = PatternFill("solid", fgColor="1F3864")
HDR_F = Font(color="FFFFFF", bold=True)
RED = PatternFill("solid", fgColor="F8CBAD")
AMBER = PatternFill("solid", fgColor="FFE699")
GREEN = PatternFill("solid", fgColor="C6E0B4")
GREY = PatternFill("solid", fgColor="D9D9D9")

NOT_A_GAP = {"http_404", "http_410", "soft_404", "skipped_gone", "trap", "too_large",
             "skipped_blocklisted", "blocked_by_robots"}
DECISION = {"antibot", "cloudflare_interactive", "needs_network_path", "auth_wall", "paywall",
            "geo_block", "ssl", "needs_captcha_solver"}
RENDER_REASONS = {"consent_wall", "render_crash", "render_timeout", "js_render"}

# Measured on this fleet 2026-08-03 — stated so the estimates can be checked, not believed.
RATE_HTTPX, RATE_RENDER = 3.5, 0.4

WHY = {
    "host_busy": "OUR limit, not their site: the per-host politeness slot never freed inside the "
                 "deflect budget. The pages are fetchable — this is scheduling, not blocking.",
    "consent_wall": "A cookie/CMP or age gate answered HTTP 200 with an interstitial instead of "
                    "the page. C3/CamoFox clears these; we never spent that tier here.",
    "render_crash": "The browser died on the page. Mostly transient.",
    "render_timeout": "The render never settled inside the watchdog.",
    "js_render": "A JS shell was served and the content never materialised.",
    "needs_network_path": "httpx, real-Chrome JA3 and C3 stealth ALL failed with no identifiable "
                          "vendor wall. Needs a different egress (residential / in-country).",
    "antibot": "A bot-management vendor (Akamai / Imperva / DataDome / PerimeterX) refused the "
               "client. Needs the C4 host solver or a residential egress.",
    "cloudflare_interactive": "Cloudflare's interactive tier — never self-clears in a container. "
                              "C4 host solver only.",
    "auth_wall": "A login / SSO gate. Needs seeded credentials; no retry opens it.",
    "paywall": "A subscriber gate. Needs a subscription.",
    "geo_block": "Withheld by country or legal order. Needs an egress in a permitted country.",
    "ssl": "Certificate / handshake refused. NOTE: a middlebox on this network re-signs certs for "
           "whole site categories, so some of these are untestable HERE rather than broken.",
    "needs_captcha_solver": "A CAPTCHA widget blocked the page.",
    "dns": "No DNS answer at the time. One failure is not a verdict — re-probe before deleting.",
    "timeout": "The fetch exceeded its budget.",
    "conn_refused": "Nothing accepted the connection.",
    "rate_limit": "Throttled by the origin; re-run slower.",
}


def _fmt(h_pages: float, r_pages: float) -> str:
    s = h_pages / RATE_HTTPX + r_pages / RATE_RENDER
    if s < 60:
        return "< 1 min"
    if s < 3600:
        return "%d min" % round(s / 60)
    if s < 86400:
        return "%.1f hours" % (s / 3600)
    return "%.1f days" % (s / 86400)


def build(orig_p, cat_p, persite_p, corpus_p, out_p):
    orig = json.load(io.open(orig_p, encoding="utf-8"))
    cat = json.load(io.open(cat_p, encoding="utf-8"))
    corpus = json.load(io.open(corpus_p, encoding="utf-8"))
    fails = {s["domain"]: s["all"] for s in json.load(io.open(persite_p, encoding="utf-8"))["per_site"]}

    rows, tally = [], Counter()
    for m in orig:
        d = m["domain"]
        f = fails.get(d, {})
        rec = {r: n for r, n in f.items() if r not in NOT_A_GAP}
        solv = sum(n for r, n in rec.items() if r not in DECISION)
        dec = sum(n for r, n in rec.items() if r in DECISION)
        rend = sum(n for r, n in rec.items() if r in RENDER_REASONS)
        cheap = solv - rend
        pages = corpus.get(d, {}).get("pages", 0)
        chars = corpus.get(d, {}).get("chars", 0)
        pending = cat.get(d, {}).get("pending", 0)
        in_cat = d in cat
        top = sorted(rec.items(), key=lambda kv: -kv[1])[:3]
        t1 = top[0][0] if top else ""

        if not in_cat:
            verdict = ("REMOVED — but we hold pages" if pages else "REMOVED from the catalogue")
        elif pages == 0:
            verdict = "COULD NOT OPEN AT ALL"
        elif dec and dec >= solv:
            verdict = "BLOCKED — needs a decision"
        elif solv or pending:
            verdict = "PARTIAL — solvable by re-running"
        else:
            verdict = "FULLY CRAWLED"
        tally[verdict] += 1

        if verdict.startswith("REMOVED"):
            est = "n/a — not in the catalogue"
            why = ("Dropped during the project (non-defence, dead DNS, duplicate or re-pointed to "
                   "another domain). Pages shown are what the corpus still holds for it.")
        elif verdict == "FULLY CRAWLED":
            est, why = "—", "Frontier exhausted with no recoverable failures left."
        elif verdict == "BLOCKED — needs a decision":
            est, why = "not a time problem", WHY.get(t1, "")
        else:
            est, why = _fmt(cheap + pending, rend), WHY.get(t1, "")

        rows.append([m["url"], m["company"], m["country"], m["confidence"], verdict, pages,
                     chars, pending, solv, dec, t1, top[0][1] if top else 0, why, est,
                     ", ".join("%s:%d" % (r, n) for r, n in top), m["cat"]])

    order = {"COULD NOT OPEN AT ALL": 0, "BLOCKED — needs a decision": 1,
             "PARTIAL — solvable by re-running": 2, "REMOVED — but we hold pages": 3,
             "REMOVED from the catalogue": 4, "FULLY CRAWLED": 5}
    rows.sort(key=lambda r: (order[r[4]], -(r[8] + r[9])))

    wb = Workbook()
    ws = wb.active
    ws.title = "1 every original url"
    ws.append(["Website (as supplied)", "Company", "Country", "Confidence", "Verdict",
               "Pages stored", "Text chars", "Still queued", "Solvable URLs",
               "Needs-decision URLs", "Main reason", "#", "Why", "Est. time to finish",
               "Reason mix (top 3)", "Original category"])
    for c in ws[1]:
        c.fill, c.font = HDR, HDR_F
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    fillmap = {"COULD NOT OPEN AT ALL": RED, "BLOCKED — needs a decision": RED,
               "PARTIAL — solvable by re-running": AMBER, "FULLY CRAWLED": GREEN,
               "REMOVED — but we hold pages": GREY, "REMOVED from the catalogue": GREY}
    for r in rows:
        ws.append(r)
        ws.cell(ws.max_row, 5).fill = fillmap[r[4]]
        ws.cell(ws.max_row, 13).alignment = Alignment(wrap_text=True, vertical="top")
    for i, w in enumerate([44, 30, 16, 11, 30, 12, 12, 12, 13, 17, 22, 8, 68, 18, 34, 24], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── summary ───────────────────────────────────────────────────────────────────────────────
    ws = wb.create_sheet("2 summary", 0)
    kept = [r for r in rows if not r[4].startswith("REMOVED")]
    solv_t = sum(r[8] for r in kept)
    dec_t = sum(r[9] for r in kept)
    pend_t = sum(r[7] for r in kept)
    rend_t = sum(n for m in orig if m["domain"] in cat
                 for r, n in fails.get(m["domain"], {}).items() if r in RENDER_REASONS)
    cheap_t = solv_t - rend_t
    for line in [
        ["Manufacturer coverage — measured against the ORIGINAL spreadsheet"],
        ["source: final urls (2).xlsx, sheet 'Manufacturer'"],
        [],
        ["Rows in the Manufacturer sheet", 883],
        ["Distinct websites (duplicates collapsed)", len(orig)],
        ["  still in the catalogue", len([r for r in rows if not r[4].startswith("REMOVED")])],
        ["  removed during the project", len([r for r in rows if r[4].startswith("REMOVED")])],
        ["    of those, we still hold pages", tally["REMOVED — but we hold pages"]],
        [],
        ["OF THE WEBSITES STILL IN THE CATALOGUE", ""],
        ["FULLY CRAWLED", tally["FULLY CRAWLED"]],
        ["PARTIAL — solvable by re-running", tally["PARTIAL — solvable by re-running"]],
        ["BLOCKED — needs a decision", tally["BLOCKED — needs a decision"]],
        ["COULD NOT OPEN AT ALL", tally["COULD NOT OPEN AT ALL"]],
        [],
        ['"FULLY CRAWLED" is deliberately strict: frontier empty AND no recoverable failures left.'],
        ["An empty frontier alone is not enough — rohde-schwarz.com has one and 119,314 URLs"],
        ["dropped on host_busy. Zero pending is also what an abandoned crawl looks like, which is"],
        ["how 564 sites were once called complete when 232 were."],
        [],
        ["WORK OUTSTANDING (catalogued sites only)", ""],
        ["URLs solvable by re-running", solv_t],
        ["  cheap httpx lane", cheap_t],
        ["  render / C3 lane", rend_t],
        ["Still queued in the frontier", pend_t],
        ["URLs needing a decision (no retry helps)", dec_t],
        [],
        ["TIME ESTIMATE", ""],
        ["Cheap lane at %.1f pages/s (measured)" % RATE_HTTPX, _fmt(cheap_t + pend_t, 0)],
        ["Render / C3 lane at %.1f pages/s (governor-limited)" % RATE_RENDER, _fmt(0, rend_t)],
        ["Both together", _fmt(cheap_t + pend_t, rend_t)],
        [],
        ["Planning figures from measured rates, not promises. The render number is the soft one:"],
        ["the CPU governor held concurrent renders to 4-6 under load, so it moves with instance"],
        ["count and with whatever else shares the box."],
        [],
        ["ORDER I WOULD RUN THEM IN", ""],
        ["1. consent_wall on the worst hosts", "Largest single recoverable block, and C3 (which "
                                               "clears it) is already running and paid for."],
        ["2. host_busy re-run", "Pure scheduling — fewer sites per instance and it collects itself."],
        ["3. render_crash / render_timeout", "Mostly transient; a second pass takes most of it."],
        ["4. antibot / needs_network_path", "Only once C4 is reliable — needs the scheduled task."],
        ["5. auth_wall / paywall / geo_block", "Not crawler work: credentials, subscriptions, or "
                                               "accept the gap."],
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=14)
    for r in (10, 21, 28, 37):
        ws.cell(r, 1).font = Font(bold=True)
    for row in ws.iter_rows():
        if len(row) > 1 and isinstance(row[1].value, str):
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 54
    ws.column_dimensions["B"].width = 88

    wb.save(out_p)
    print("wrote %s" % out_p)
    print("  original websites : %d" % len(orig))
    for k in ("FULLY CRAWLED", "PARTIAL — solvable by re-running", "BLOCKED — needs a decision",
              "COULD NOT OPEN AT ALL", "REMOVED — but we hold pages", "REMOVED from the catalogue"):
        print("  %-34s %d" % (k, tally[k]))


if __name__ == "__main__":
    build(*sys.argv[1:6])
