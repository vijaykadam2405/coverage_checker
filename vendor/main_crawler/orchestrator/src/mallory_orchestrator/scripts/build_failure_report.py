"""Build the manufacturer coverage-failure report.

WHAT THIS ANSWERS: of the catalogued manufacturers, which URLs did the crawler fail to open, and
why — split so the operator can act, because the reasons are NOT equivalent:

  * a scheduling limit we imposed on ourselves (host_busy) is not a site defect at all;
  * a consent/CMP wall is a page we CAN have, on a tier we did not spend;
  * an antibot or interactive-Cloudflare wall needs a different egress or the C4 host solver;
  * a 404/410 is correct behaviour and should not be counted as a gap.

Collapsing those into one "failed" number is what made "564 complete" mean 232, so every sheet
here keeps the reason attached to the count.

Inputs are produced on the server (read-only over the 12 shared instance histories):
    persite.json   per-domain reason counts + stored page counts
    samples.json   up to 25 example URLs per reason + the crawler's own reason explanations

    python build_failure_report.py persite.json samples.json out.xlsx
"""
from __future__ import annotations

import json
import sys
from collections import Counter

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HDR = PatternFill("solid", fgColor="1F3864")
HDR_F = Font(color="FFFFFF", bold=True)
BAD = PatternFill("solid", fgColor="F8CBAD")      # terminal — needs a decision, not a retry
WARN = PatternFill("solid", fgColor="FFE699")     # retryable — a re-run or a tier can fix it
OK = PatternFill("solid", fgColor="C6E0B4")

# Operator-facing meaning. The crawler's own _REASON_DETAIL is merged in on top of these where it
# has an entry; these fill the gaps and say what ACTION changes the outcome, which is the thing an
# operator actually needs and the raw reason never says.
ACTION = {
    "host_busy": ("OUR LIMIT, NOT THEIR SITE. The per-host politeness slot never freed within the "
                  "deflect budget, so the URL was dropped for this run. The page is almost "
                  "certainly fetchable.", "Re-run the site with fewer sites per instance, or raise "
                  "CRAWLER_HOST_DEFLECT_BUDGET_S. Nothing about the site needs fixing."),
    "consent_wall": ("A cookie/CMP or age gate answered 200 with an interstitial instead of the "
                     "page. The content exists and we never saw it.", "Re-fetch through C3 "
                     "(CamoFox clears CMPs). These are RETRYABLE and currently the single largest "
                     "recoverable block in the corpus."),
    "needs_network_path": ("Blocked after httpx + real-Chrome JA3 + C3 stealth all failed, with no "
                           "vendor wall we could identify.", "Needs a different egress "
                           "(residential/in-country) or the C4 host solver. Do not retry as-is."),
    "antibot": ("A bot-management vendor (Akamai/Imperva/DataDome/PerimeterX) refused the client.",
                "C4 host solver, or a residential egress. A plain retry will not clear it."),
    "cloudflare_interactive": ("Cloudflare's INTERACTIVE tier — never self-clears and injects no "
                              "widget, so nothing in-container can pass it.",
                              "C4 host solver only. This is exactly what C4 exists for."),
    "auth_wall": ("A login/SSO gate. The page is real but requires an account.",
                  "Seed credentials for the host, or accept the gap. A retry cannot help."),
    "paywall": ("A subscriber gate.", "Accept the gap unless a subscription exists."),
    "geo_block": ("Withheld by country or legal order (451, or a geo body on a 200).",
                  "Needs an egress in a permitted country. Not fixable from here."),
    "needs_captcha_solver": ("A CAPTCHA widget stood between us and the page.",
                             "The local VLM solves image-text captchas; commercial widgets need a "
                             "provider key."),
    "render_crash": ("The browser died on this page.", "Retryable; if it repeats on one host, that "
                     "host needs a look."),
    "render_timeout": ("The render never reached a settled state inside the watchdog.",
                       "Retryable. Persistent cases usually mean an endless-loading page."),
    "js_render": ("A JS shell was served and the content never materialised.",
                  "Retryable through the render tier."),
    "http_404": ("The page is genuinely gone.", "CORRECT BEHAVIOUR — not a coverage gap. Excluded "
                 "from the recoverable totals."),
    "http_410": ("Explicitly gone.", "CORRECT BEHAVIOUR — not a coverage gap."),
    "soft_404": ("A 200 that is really a 'not found' page.", "Not a coverage gap."),
    "skipped_gone": ("Known 404/410 from a previous run, skipped on purpose.", "Not a gap."),
    "trap": ("Dropped by the frontier trap heuristic (calendar/generator/nav permutation).",
             "Intentional. Counting these as gaps is how a menu permuting itself once looked like "
             "24,244 pages of work."),
    "rate_limit": ("Throttled by the origin.", "Retryable at a slower rate."),
    "dns": ("No DNS answer.", "Re-probe; a single failure is not a verdict (174 live sites were "
            "once called dead this way)."),
    "ssl": ("Certificate/handshake refused — never bypassed automatically.",
            "Per-host tls_insecure only after a human looks at the cert."),
    "conn_refused": ("Nothing accepted the connection.", "Retryable."),
    "timeout": ("The fetch exceeded its budget.", "Retryable."),
    "too_large": ("Body above the size cap.", "Intentional."),
}
# Reasons that are CORRECT behaviour, not coverage gaps. Kept separate so the headline number
# means "pages we could have had and do not".
NOT_A_GAP = {"http_404", "http_410", "soft_404", "skipped_gone", "trap", "too_large",
             "skipped_blocklisted", "blocked_by_robots"}


def _autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _header(ws, cols):
    ws.append(cols)
    for c in ws[1]:
        c.fill, c.font = HDR, HDR_F
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def build(persite_path: str, samples_path: str, out_path: str) -> None:
    ps = json.load(open(persite_path, encoding="utf-8"))
    sm = json.load(open(samples_path, encoding="utf-8"))
    sites, pages = ps["per_site"], ps.get("pages", {})
    samples, rdetail = sm["samples"], sm.get("reason_detail", {})

    totals = Counter()
    for s in sites:
        for r, n in s["all"].items():
            totals[r] += n
    recoverable = sum(n for r, n in totals.items() if r not in NOT_A_GAP)

    wb = Workbook()

    # ── 1. reasons ────────────────────────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "1 reasons"
    _header(ws, ["Reason", "URLs", "% of all", "Is it a coverage gap?", "What it means",
                 "What actually fixes it"])
    for r, n in totals.most_common():
        gap = "no — correct behaviour" if r in NOT_A_GAP else "YES"
        mean, fix = ACTION.get(r, ("", ""))
        if r in rdetail and not mean:
            mean = rdetail[r][:400]
        ws.append([r, n, round(100.0 * n / max(1, sum(totals.values())), 2), gap, mean, fix])
        ws.cell(ws.max_row, 4).fill = OK if r in NOT_A_GAP else (
            WARN if r in ("host_busy", "consent_wall", "render_crash", "render_timeout",
                          "js_render", "timeout", "conn_refused", "dns", "rate_limit",
                          "needs_captcha_solver") else BAD)
    for row in ws.iter_rows(min_row=2):
        row[4].alignment = row[5].alignment = Alignment(wrap_text=True, vertical="top")
    _autosize(ws, [26, 10, 9, 22, 62, 62])

    # ── 2. per site ───────────────────────────────────────────────────────────────────────────
    ws = wb.create_sheet("2 per site")
    _header(ws, ["Domain", "Pages stored", "Failed URLs", "Recoverable", "Not a gap",
                 "Top reason", "#", "2nd reason", "#", "3rd reason", "#"])
    for s in sorted(sites, key=lambda x: -sum(n for r, n in x["all"].items() if r not in NOT_A_GAP)):
        rec = sum(n for r, n in s["all"].items() if r not in NOT_A_GAP)
        nog = s["failed_urls"] - rec
        top = [(r, n) for r, n in sorted(s["all"].items(), key=lambda kv: -kv[1])][:3]
        while len(top) < 3:
            top.append(("", ""))
        ws.append([s["domain"], s["pages"], s["failed_urls"], rec, nog,
                   top[0][0], top[0][1], top[1][0], top[1][1], top[2][0], top[2][1]])
        if rec > 1000:
            ws.cell(ws.max_row, 4).fill = BAD
        elif rec > 100:
            ws.cell(ws.max_row, 4).fill = WARN
    _autosize(ws, [34, 13, 12, 13, 11, 22, 8, 22, 8, 22, 8])

    # ── 3. example urls ───────────────────────────────────────────────────────────────────────
    ws = wb.create_sheet("3 example urls")
    _header(ws, ["Reason", "URL", "HTTP", "Attempts", "Instance", "Error detail"])
    for r, n in totals.most_common():
        for e in samples.get(r, []):
            ws.append([r, e.get("url", ""), e.get("status"), e.get("fails"),
                       e.get("instance"), (e.get("detail") or "")[:200]])
    _autosize(ws, [24, 84, 8, 10, 10, 60])

    # ── 4. read me ────────────────────────────────────────────────────────────────────────────
    ws = wb.create_sheet("4 read me")
    for line in [
        ["Manufacturer coverage-failure report"],
        [],
        ["Every URL below is one the crawler finished with an error recorded against it."],
        ["crawl_pages is keyed by canonical URL and the error is CLEARED on a later success, so"],
        ["each row is a distinct URL whose LAST attempt failed."],
        [],
        ["Failed URLs (all reasons)", sum(totals.values())],
        ["Of those, real coverage gaps", recoverable],
        ["Correct behaviour (404/410/soft-404/traps)", sum(totals.values()) - recoverable],
        ["Hosts with at least one failure", len(sites)],
        ["Catalogued sources holding ZERO pages", sum(1 for v in pages.values() if v == 0)],
        [],
        ["THE TWO THAT DOMINATE, AND WHY THEY ARE NOT SITE DEFECTS"],
        ["host_busy", "A politeness slot we never freed inside the deflect budget. Our scheduling "
                      "limit, not their site. Almost all of it is fetchable on a re-run."],
        ["consent_wall", "A cookie/CMP or age gate answered 200 with an interstitial. The content "
                         "exists and was never seen. C3 clears these, so it is the largest single "
                         "recoverable block in the corpus."],
        [],
        ["WHAT NEEDS A DECISION RATHER THAN A RE-RUN"],
        ["antibot / cloudflare_interactive", "C4 host solver or a residential egress."],
        ["auth_wall / paywall", "Credentials, or accept the gap."],
        ["geo_block", "An egress in a permitted country."],
        ["needs_network_path", "Everything cheap already failed; a different egress is the "
                               "remaining lever."],
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=14)
    for r in (13, 18):
        ws.cell(r, 1).font = Font(bold=True)
    for row in ws.iter_rows(min_row=1):
        if len(row) > 1 and row[1].value:
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    _autosize(ws, [38, 96])

    wb.save(out_path)
    print("wrote %s" % out_path)
    print("  failed urls      : %d" % sum(totals.values()))
    print("  real gaps        : %d" % recoverable)
    print("  correct behaviour: %d" % (sum(totals.values()) - recoverable))
    print("  hosts            : %d" % len(sites))


if __name__ == "__main__":
    build(*(sys.argv[1:4] if len(sys.argv) >= 4
            else ("persite.json", "samples.json", "manufacturer_failures.xlsx")))
