"""All 805 original manufacturer websites, one row each: done, partial, blocked, or removed.

Deliberately plain. The earlier reports carry the per-URL forensics; this one answers the only
question that matters at a glance — for each website we selected, did we get it, how much is left,
and if we cannot have it, why.

"REMAINING" is an estimate and is built from two real things, not a guess:
    pending   URLs still sitting in that site's durable frontier
    solvable  URLs whose last attempt failed for a reason a re-run fixes (host_busy, consent_wall,
              render crashes/timeouts, transient network)
It excludes 404/410/soft-404/traps (correct behaviour, not a gap) and excludes anything needing a
decision rather than a retry (antibot, auth walls, geo blocks) — those are counted as BLOCKED so
the remaining figure stays honest about what re-running would actually collect.

    python build_805_status.py original_manufacturers.json manufacturers.json persite.json \
           corpuspages.json out.xlsx
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
GREEN = PatternFill("solid", fgColor="C6E0B4")
AMBER = PatternFill("solid", fgColor="FFE699")
RED = PatternFill("solid", fgColor="F8CBAD")
GREY = PatternFill("solid", fgColor="D9D9D9")

NOT_A_GAP = {"http_404", "http_410", "soft_404", "skipped_gone", "trap", "too_large",
             "skipped_blocklisted", "blocked_by_robots"}
DECISION = {"antibot", "cloudflare_interactive", "needs_network_path", "auth_wall", "paywall",
            "geo_block", "ssl", "needs_captcha_solver"}
RENDER = {"consent_wall", "render_crash", "render_timeout", "js_render"}
RATE_HTTPX, RATE_RENDER = 3.5, 0.4          # measured on this fleet, 2026-08-03

REASON = {
    "host_busy": "Our own politeness budget ran out waiting for a slot — the site never refused "
                 "us. Re-runs at full speed.",
    "consent_wall": "Cookie/CMP or age gate returned HTTP 200 with an interstitial instead of the "
                    "page. C3 clears it; we never spent that tier here.",
    "render_crash": "The browser died on these pages. Mostly transient.",
    "render_timeout": "The render never settled inside the watchdog.",
    "js_render": "A JS shell was served and the content never appeared.",
    "timeout": "Fetches exceeded their time budget.",
    "conn_refused": "Nothing accepted the connection.",
    "dns": "No DNS answer at the time. Re-probe before believing it.",
    "rate_limit": "Throttled by the origin; needs a slower re-run.",
    "needs_network_path": "httpx, real-Chrome JA3 and C3 stealth ALL failed with no identifiable "
                          "wall. Needs a different egress (residential / in-country).",
    "antibot": "A bot-management vendor (Akamai/Imperva/DataDome/PerimeterX) refused us. Needs the "
               "C4 host solver or a residential egress.",
    "cloudflare_interactive": "Cloudflare interactive challenge — never clears from a container. "
                              "C4 host solver only.",
    "auth_wall": "Login / SSO gate. Needs credentials; no retry opens it.",
    "paywall": "Subscriber gate. Needs a subscription.",
    "geo_block": "Withheld by country or legal order. Needs an egress in a permitted country.",
    "ssl": "Certificate/handshake refused. NOTE: a middlebox on this network re-signs certs for "
           "whole categories, so this may be untestable HERE rather than broken.",
    "needs_captcha_solver": "A CAPTCHA widget blocked the pages.",
}


def _time(cheap, rend):
    s = cheap / RATE_HTTPX + rend / RATE_RENDER
    if s <= 0:
        return "—"
    if s < 3600:
        return "%d min" % max(1, round(s / 60))
    if s < 86400:
        return "%.1f hours" % (s / 3600)
    return "%.1f days" % (s / 86400)


def build(orig_p, cat_p, persite_p, corpus_p, out_p):
    orig = json.load(io.open(orig_p, encoding="utf-8"))
    cat = json.load(io.open(cat_p, encoding="utf-8"))
    corpus = json.load(io.open(corpus_p, encoding="utf-8"))
    fails = {s["domain"]: s["all"]
             for s in json.load(io.open(persite_p, encoding="utf-8"))["per_site"]}

    rows, tally, pg = [], Counter(), Counter()
    tot = Counter()
    for m in orig:
        d = m["domain"]
        f = fails.get(d, {})
        rec = {r: n for r, n in f.items() if r not in NOT_A_GAP}
        solvable = sum(n for r, n in rec.items() if r not in DECISION)
        blocked = sum(n for r, n in rec.items() if r in DECISION)
        rend = sum(n for r, n in rec.items() if r in RENDER)
        cheap = solvable - rend
        pages = corpus.get(d, {}).get("pages", 0)
        pending = cat.get(d, {}).get("pending", 0)
        remaining = solvable + pending
        top = sorted(rec.items(), key=lambda kv: -kv[1])
        t1 = top[0][0] if top else ""

        if d not in cat:
            status, why, rem, est = ("REMOVED", "Dropped from the catalogue during the project "
                                     "(non-defence, dead, duplicate or re-pointed). No pages held.",
                                     0, "—")
        elif pages == 0:
            status = "NOT POSSIBLE"
            why = REASON.get(t1, "Could not be opened by any tier (C1/C3/C4).")
            rem, est = 0, "—"
        elif blocked and blocked >= solvable:
            status = "BLOCKED"
            why = REASON.get(t1, t1)
            rem, est = 0, "not a time problem"
        elif remaining > 0:
            status = "PARTIAL"
            why = REASON.get(t1, t1)
            rem, est = remaining, _time(cheap + pending, rend)
        else:
            status, why, rem, est = "COMPLETE", "", 0, "—"

        tally[status] += 1
        pg[status] += pages
        tot["remaining"] += rem
        tot["blocked_urls"] += blocked if status in ("BLOCKED", "PARTIAL") else 0
        tot["cheap"] += cheap if status == "PARTIAL" else 0
        tot["render"] += rend if status == "PARTIAL" else 0
        tot["pending"] += pending if status == "PARTIAL" else 0
        rows.append([m["url"], m["company"], m["country"], status, pages, rem, est, why,
                     blocked, ", ".join("%s:%d" % (r, n) for r, n in top[:3])])

    order = {"NOT POSSIBLE": 0, "BLOCKED": 1, "PARTIAL": 2, "COMPLETE": 3, "REMOVED": 4}
    rows.sort(key=lambda r: (order[r[3]], -r[5], -r[4]))

    wb = Workbook()

    # ── sheet 1: every website ────────────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "All 805 websites"
    ws.append(["Website", "Company", "Country", "Status", "Pages done", "Pages remaining (est)",
               "Time to finish", "Reason / why", "Blocked URLs", "Detail"])
    for c in ws[1]:
        c.fill, c.font = HDR, HDR_F
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    fill = {"COMPLETE": GREEN, "PARTIAL": AMBER, "BLOCKED": RED, "NOT POSSIBLE": RED,
            "REMOVED": GREY}
    for r in rows:
        ws.append(r)
        ws.cell(ws.max_row, 4).fill = fill[r[3]]
        ws.cell(ws.max_row, 8).alignment = Alignment(wrap_text=True, vertical="top")
    for i, w in enumerate([42, 30, 16, 15, 12, 20, 15, 66, 12, 32], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── sheet 2: summary ──────────────────────────────────────────────────────────────────────
    ws = wb.create_sheet("Summary", 0)
    live = sum(tally[k] for k in ("COMPLETE", "PARTIAL", "BLOCKED", "NOT POSSIBLE"))
    donepg = sum(pg[k] for k in ("COMPLETE", "PARTIAL", "BLOCKED", "NOT POSSIBLE"))
    for line in [
        ["Manufacturer coverage — all 805 websites from final urls (2).xlsx"],
        [],
        ["WEBSITES", "count", "pages held"],
        ["COMPLETE", tally["COMPLETE"], pg["COMPLETE"]],
        ["PARTIAL", tally["PARTIAL"], pg["PARTIAL"]],
        ["BLOCKED", tally["BLOCKED"], pg["BLOCKED"]],
        ["NOT POSSIBLE", tally["NOT POSSIBLE"], pg["NOT POSSIBLE"]],
        ["REMOVED from catalogue", tally["REMOVED"], pg["REMOVED"]],
        ["TOTAL", sum(tally.values()), donepg],
        [],
        ["Still in the catalogue", live],
        ["Pages collected from them", donepg],
        [],
        ["WHAT IS LEFT", ""],
        ["Pages still collectable by re-running", tot["remaining"]],
        ["   still queued in the frontier", tot["pending"]],
        ["   failed on a reason a re-run fixes", tot["remaining"] - tot["pending"]],
        ["      of which need the cheap httpx lane", tot["cheap"]],
        ["      of which need the render / C3 lane", tot["render"]],
        ["Pages behind a wall a retry cannot pass", tot["blocked_urls"]],
        [],
        ["TIME TO FINISH THE RE-RUNNABLE WORK", ""],
        ["Cheap lane at %.1f pages/s (measured)" % RATE_HTTPX, _time(tot["cheap"] + tot["pending"], 0)],
        ["Render / C3 lane at %.1f pages/s (governor-limited)" % RATE_RENDER, _time(0, tot["render"])],
        ["Both run together", _time(tot["cheap"] + tot["pending"], tot["render"])],
        [],
        ["HOW TO READ THIS", ""],
        ["COMPLETE", "Frontier exhausted AND nothing left that a re-run would collect. Strict on "
                     "purpose: an empty frontier alone is also what an abandoned crawl looks like."],
        ["PARTIAL", "We hold pages and more are reachable. 'Remaining' = queued URLs + URLs whose "
                    "last attempt failed for a fixable reason."],
        ["BLOCKED", "Most of what is missing sits behind a wall no retry passes — antibot, login, "
                    "geo, or an egress problem. Needs a decision, not more crawling."],
        ["NOT POSSIBLE", "No tier (C1 httpx / C3 stealth / C4 host solver) ever opened it."],
        ["REMOVED", "Dropped from the catalogue during the project. We hold no pages for these."],
        [],
        ["THE THING WORTH NOTICING", ""],
        ["Completed sites are the SMALL ones", "COMPLETE averages %d pages/site; PARTIAL averages "
         "%d. Sites finish because they are small — the large manufacturers are all still "
         "incomplete, so the remaining work is most of the remaining value."
         % (pg["COMPLETE"] // max(1, tally["COMPLETE"]), pg["PARTIAL"] // max(1, tally["PARTIAL"]))],
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=14)
    for r in (3, 14, 22, 27, 34):
        for c in ws[r]:
            c.font = Font(bold=True)
    for row in ws.iter_rows():
        if len(row) > 1 and isinstance(row[1].value, str):
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 90
    ws.column_dimensions["C"].width = 14

    wb.save(out_p)
    print("wrote %s" % out_p)
    for k in ("COMPLETE", "PARTIAL", "BLOCKED", "NOT POSSIBLE", "REMOVED"):
        print("  %-16s %4d sites   %8d pages" % (k, tally[k], pg[k]))
    print("  remaining collectable pages: %d (cheap %d / render %d / queued %d)"
          % (tot["remaining"], tot["cheap"], tot["render"], tot["pending"]))


if __name__ == "__main__":
    build(*sys.argv[1:6])
