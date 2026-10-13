"""Add the SITE-LEVEL verdict sheet to the manufacturer failure report.

The per-URL sheets answer "what went wrong"; this one answers the question an operator actually
has: for each catalogued manufacturer WEBSITE — not page — did we get it, can we still get it, and
how long would that take?

THE DEFINITION OF "FULLY CRAWLED" MATTERS AND IS DELIBERATELY STRICT. An exhausted frontier alone
is not enough: rohde-schwarz.com has an empty frontier and 119,314 URLs dropped on host_busy. Zero
pending means "nothing queued", which is also what a site abandoned mid-crawl looks like. So a site
counts as fully crawled only when its frontier is empty AND it has no recoverable failures left.
That is the same trap that once let 564 sites be called complete when 232 were.

    python add_site_verdicts.py persite.json manufacturers.json report.xlsx
"""
from __future__ import annotations

import json
import sys
from collections import Counter

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HDR = PatternFill("solid", fgColor="1F3864")
HDR_F = Font(color="FFFFFF", bold=True)
RED = PatternFill("solid", fgColor="F8CBAD")
AMBER = PatternFill("solid", fgColor="FFE699")
GREEN = PatternFill("solid", fgColor="C6E0B4")

# Correct behaviour — never a coverage gap.
NOT_A_GAP = {"http_404", "http_410", "soft_404", "skipped_gone", "trap", "too_large",
             "skipped_blocklisted", "blocked_by_robots"}
# Cannot be fixed by re-running; needs an egress, a credential or a decision.
DECISION = {"antibot", "cloudflare_interactive", "needs_network_path", "auth_wall", "paywall",
            "geo_block", "ssl", "needs_captcha_solver"}
# Fixed by re-running on the right tier.
SOLVABLE = {"host_busy", "consent_wall", "render_crash", "render_timeout", "js_render", "timeout",
            "conn_refused", "dns", "rate_limit", "http_500", "http_502", "http_503", "http_504",
            "http_520", "http_522", "http_523", "http_530"}

# MEASURED on this fleet, and stated so the estimates can be checked rather than believed.
#   cheap lane: 3.5 fetches/s fleet-wide sustained (12 instances, 2026-08-03)
#   render lane: the governor allowed 4-6 concurrent renders under load; a stealth render is
#                ~10-20s, so ~0.4 pages/s fleet-wide is the honest planning figure.
RATE_HTTPX = 3.5
RATE_RENDER = 0.4
RENDER_REASONS = {"consent_wall", "render_crash", "render_timeout", "js_render"}

WHY = {
    "host_busy": "Our own politeness budget expired waiting for a per-host slot — the site never "
                 "refused us. Re-runs at full speed with fewer sites per instance.",
    "consent_wall": "A cookie/CMP or age gate returned HTTP 200 with an interstitial instead of "
                    "the page. C3/CamoFox clears these; we simply never spent that tier here.",
    "render_crash": "The browser died on the page. Transient in most cases.",
    "render_timeout": "The render never settled inside the watchdog.",
    "js_render": "A JS shell was served and the content never materialised.",
    "needs_network_path": "httpx, real-Chrome JA3 and C3 stealth ALL failed with no identifiable "
                          "vendor wall. Needs a different egress — residential or in-country.",
    "antibot": "A bot-management vendor (Akamai/Imperva/DataDome/PerimeterX) refused the client. "
               "Needs the C4 host solver or a residential egress.",
    "cloudflare_interactive": "Cloudflare's interactive tier — never self-clears in-container. "
                              "C4 host solver only.",
    "auth_wall": "A login/SSO gate. Needs seeded credentials; no retry opens it.",
    "paywall": "A subscriber gate. Needs a subscription.",
    "geo_block": "Withheld by country or legal order. Needs an egress in a permitted country.",
    "ssl": "Certificate/handshake refused. NOTE: a middlebox on this network re-signs certs for "
           "whole site categories, so some of these are untestable here rather than broken.",
    "needs_captcha_solver": "A CAPTCHA widget blocked the page.",
    "dns": "No DNS answer at the time. A single failure is not a verdict — re-probe.",
    "timeout": "The fetch exceeded its budget.",
    "conn_refused": "Nothing accepted the connection.",
    "rate_limit": "Throttled by the origin; re-run slower.",
}


def _fmt_hours(pages_httpx: float, pages_render: float) -> str:
    secs = pages_httpx / RATE_HTTPX + pages_render / RATE_RENDER
    if secs < 60:
        return "< 1 min"
    if secs < 3600:
        return "%d min" % round(secs / 60)
    if secs < 86400:
        return "%.1f hours" % (secs / 3600)
    return "%.1f days" % (secs / 86400)


def build(persite_path: str, man_path: str, xlsx_path: str) -> None:
    ps = json.load(open(persite_path, encoding="utf-8"))
    man = json.load(open(man_path, encoding="utf-8"))
    fails = {s["domain"]: s["all"] for s in ps["per_site"]}

    rows, tally = [], Counter()
    for dom, m in man.items():
        f = fails.get(dom, {})
        rec = {r: n for r, n in f.items() if r not in NOT_A_GAP}
        solv = sum(n for r, n in rec.items() if r in SOLVABLE)
        dec = sum(n for r, n in rec.items() if r in DECISION)
        render_n = sum(n for r, n in rec.items() if r in RENDER_REASONS)
        httpx_n = solv - render_n
        pages, pending = m["pages"], m["pending"]
        top = sorted(rec.items(), key=lambda kv: -kv[1])[:3]
        top1 = top[0][0] if top else ""

        if pages == 0:
            verdict = "COULD NOT OPEN AT ALL"
        elif dec and dec >= solv:
            verdict = "BLOCKED — needs a decision"
        elif solv or pending:
            verdict = "PARTIAL — solvable by re-running"
        else:
            verdict = "FULLY CRAWLED"
        tally[verdict] += 1

        est = "—" if verdict == "FULLY CRAWLED" else (
            "not a time problem" if verdict == "BLOCKED — needs a decision"
            else _fmt_hours(httpx_n + pending, render_n))
        rows.append({
            "domain": dom, "url": m.get("seed") or "https://%s/" % dom,
            "region": m.get("region", ""), "verdict": verdict, "pages": pages,
            "pending": pending, "solvable": solv, "decision": dec,
            "top": top1, "topn": top[0][1] if top else 0,
            "why": WHY.get(top1, "") if top1 else
                   ("Frontier exhausted with no recoverable failures left."
                    if verdict == "FULLY CRAWLED" else ""),
            "est": est,
            "mix": ", ".join("%s:%d" % (r, n) for r, n in top),
        })

    order = {"COULD NOT OPEN AT ALL": 0, "BLOCKED — needs a decision": 1,
             "PARTIAL — solvable by re-running": 2, "FULLY CRAWLED": 3}
    rows.sort(key=lambda r: (order[r["verdict"]], -(r["solvable"] + r["decision"])))

    wb = load_workbook(xlsx_path)
    for name in ("5 site verdicts", "6 what to run"):
        if name in wb.sheetnames:
            del wb[name]

    ws = wb.create_sheet("5 site verdicts", 0)
    ws.append(["Website", "Verdict", "Pages stored", "Still queued", "Solvable URLs",
               "Needs-decision URLs", "Main reason", "#", "Why", "Est. time to finish",
               "Reason mix (top 3)", "Region"])
    for c in ws[1]:
        c.fill, c.font = HDR, HDR_F
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    for r in rows:
        ws.append([r["url"], r["verdict"], r["pages"], r["pending"], r["solvable"],
                   r["decision"], r["top"], r["topn"], r["why"], r["est"], r["mix"],
                   r["region"]])
        fill = {"COULD NOT OPEN AT ALL": RED, "BLOCKED — needs a decision": RED,
                "PARTIAL — solvable by re-running": AMBER, "FULLY CRAWLED": GREEN}[r["verdict"]]
        ws.cell(ws.max_row, 2).fill = fill
        ws.cell(ws.max_row, 9).alignment = Alignment(wrap_text=True, vertical="top")
    for i, w in enumerate([46, 30, 12, 12, 13, 17, 22, 8, 70, 18, 40, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── the answer sheet ──────────────────────────────────────────────────────────────────────
    ws = wb.create_sheet("6 what to run", 1)
    tot_solv = sum(r["solvable"] for r in rows)
    tot_pend = sum(r["pending"] for r in rows)
    tot_dec = sum(r["decision"] for r in rows)
    render_tot = sum(n for s in ps["per_site"] if s["domain"] in man
                     for r, n in s["all"].items() if r in RENDER_REASONS)
    httpx_tot = tot_solv - render_tot
    for line in [
        ["Manufacturer catalogue — where it stands"],
        [],
        ["Catalogued manufacturers now", len(man)],
        ["(the original 801 minus sources removed as non-defence / dead over the project)", ""],
        [],
        ["FULLY CRAWLED", tally["FULLY CRAWLED"]],
        ["PARTIAL — solvable by re-running", tally["PARTIAL — solvable by re-running"]],
        ["BLOCKED — needs a decision", tally["BLOCKED — needs a decision"]],
        ["COULD NOT OPEN AT ALL", tally["COULD NOT OPEN AT ALL"]],
        [],
        ['"Fully crawled" is STRICT: frontier empty AND no recoverable failures left.'],
        ["An empty frontier alone is not enough — rohde-schwarz.com has one and 119,314 URLs"],
        ["dropped on host_busy. Zero pending is also what an abandoned crawl looks like."],
        [],
        ["WORK OUTSTANDING", ""],
        ["URLs solvable by re-running", tot_solv],
        ["  of those, cheap httpx lane", httpx_tot],
        ["  of those, render/C3 lane", render_tot],
        ["Still queued in the frontier", tot_pend],
        ["URLs needing a decision (no retry helps)", tot_dec],
        [],
        ["TIME ESTIMATE", ""],
        ["Cheap lane at %.1f pages/s (measured on this fleet)" % RATE_HTTPX,
         _fmt_hours(httpx_tot + tot_pend, 0)],
        ["Render/C3 lane at %.1f pages/s (governor-limited)" % RATE_RENDER,
         _fmt_hours(0, render_tot)],
        ["Both, run together", _fmt_hours(httpx_tot + tot_pend, render_tot)],
        [],
        ["These are planning figures from measured rates, not promises. The render figure is the"],
        ["soft one: the CPU governor held concurrent renders to 4-6 under load, so it moves a lot"],
        ["with how many instances run and what else shares the box."],
        [],
        ["ORDER I WOULD RUN THEM IN", ""],
        ["1. consent_wall on the top hosts", "Biggest single recoverable block, and the tier that "
                                             "clears it (C3) is already running and paid for."],
        ["2. host_busy re-run", "Pure scheduling. Fewer sites per instance and it collects itself."],
        ["3. render_crash / render_timeout", "Transient; a second pass takes most of it."],
        ["4. antibot / needs_network_path", "Only after C4 is reliable — needs the scheduled task."],
        ["5. auth_wall / paywall / geo_block", "Not crawler work. Credentials, subscriptions, or "
                                               "accept the gap."],
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=14)
    for r in (15, 22, 31):
        ws.cell(r, 1).font = Font(bold=True)
    for row in ws.iter_rows():
        if len(row) > 1 and isinstance(row[1].value, str):
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 86

    wb.save(xlsx_path)
    print("updated %s" % xlsx_path)
    for k in ("FULLY CRAWLED", "PARTIAL — solvable by re-running",
              "BLOCKED — needs a decision", "COULD NOT OPEN AT ALL"):
        print("  %-34s %d" % (k, tally[k]))
    print("  solvable urls %d (httpx %d / render %d) · pending %d · decision %d"
          % (tot_solv, httpx_tot, render_tot, tot_pend, tot_dec))


if __name__ == "__main__":
    build(*sys.argv[1:4])
