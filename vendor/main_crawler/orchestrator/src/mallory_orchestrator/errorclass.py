"""Group a crawler reason code into the family an operator actually acts on.

The crawler records WHY each URL failed, and it records it precisely: `needs_network_path`,
`host_busy`, `http_403`, `consent_wall`, `needs_render`. That precision is the right thing to store
and the wrong thing to read — the Logs tab was a flat list of thirty-odd codes ordered by count, so
"the site is blocking us", "the page is genuinely gone" and "we decided not to fetch it" sat
interleaved, and the one question the list exists to answer — *what kind of problem do I have?* —
took reading every row.

Each family below is a DIFFERENT ACTION, which is the only test that matters for a grouping:

    blocked   the site refused us          → change the network path / solve the wall / slow down
    wall      content sits behind a gate   → credentials, or accept we cannot have it
    missing   the page is not there        → nothing to do; stop asking
    network   we never reached the origin  → retry; check DNS/TLS/our own egress
    server    the origin itself broke      → retry later, it is their fault
    render    it needed a browser          → render capacity / the C3 tier
    skipped   OUR policy declined it       → a knob WE own: robots, budgets, traps, off-peak
    unknown   unclassified                 → the bucket that must never be silently empty

`recoverable` is deliberately separate from the family, because they answer different questions and
this codebase has already been burned by conflating them: `host_busy` is a *skipped* URL and a
*retryable* one, and filing it as terminal is what buried 617 live pages as unrecoverable.

The table is a LITERAL, not an import: the orchestrator does not depend on the crawler package.
That is exactly why `tests/test_errorclass.py` pins it against `crawler/errors.py` — two vocabularies
that can drift silently need a test that fails when they do.
"""
from __future__ import annotations

# reason code -> family. `http_<n>` is handled by the status rules in classify(), not here.
_FAMILY: dict[str, str] = {
    # the site refused us
    "needs_network_path": "blocked", "needs_captcha_solver": "blocked",
    "captcha_failed": "blocked", "rate_limited": "blocked",
    # ...including by NAME of the thing that refused. `crawler/captcha.py` records the vendor it
    # fingerprinted, and those strings reach this table directly: a real crawl surfaced
    # `cloudflare_interactive` on the first try and it landed in "unclassified", which is the
    # grouping doing its job and also a gap. Every one of these means the same action — a different
    # network path, a stealth render, or a solver — so they share the blocked family and differ only
    # in whether another attempt from here could ever work.
    "cloudflare_managed": "blocked", "cloudflare_interactive": "blocked",
    "turnstile": "blocked", "recaptcha_v2": "blocked", "hcaptcha": "blocked",
    "arkose": "blocked", "geetest": "blocked", "keycaptcha": "blocked", "anubis": "blocked",
    "datadome": "blocked", "perimeterx": "blocked", "imperva": "blocked", "kasada": "blocked",
    "akamai": "blocked", "awswaf": "blocked", "servicepipe": "blocked",
    # content behind a gate
    "auth_wall": "wall", "paywall": "wall", "consent_wall": "wall", "geo_block": "wall",
    # not there
    "skipped_gone": "missing",
    # never reached the origin
    "dns": "network", "conn_refused": "network", "timeout": "network", "ssl": "network",
    "origin_ssl": "network",
    # the origin broke
    "parse_error": "server",
    # needed a browser
    "needs_render": "render", "render_crash": "render", "render_timeout": "render",
    "render_failed": "render",
    # our own policy declined it
    "blocked_by_robots": "skipped", "skipped_off_peak": "skipped", "too_large": "skipped",
    "trap": "skipped", "skipped_host_down": "skipped", "host_busy": "skipped",
    # explicitly unclassified by the crawler itself
    "other": "unknown",
}

# Families the crawler treats as worth another attempt. Mirrors errors.bucket_reason, which the
# orchestrator cannot import — see the module docstring.
_RETRYABLE = {
    "timeout", "conn_refused", "dns", "origin_ssl", "rate_limited", "consent_wall",
    "needs_render", "render_crash", "render_timeout", "render_failed", "host_busy",
    "skipped_host_down", "parse_error", "needs_network_path",
    # A MANAGED Cloudflare challenge self-clears for a coherent fingerprint, which is exactly what
    # the stealth tier provides, so it is worth another attempt. The INTERACTIVE one never
    # self-clears and nothing in the container can pass it — retrying it spends budget to learn the
    # same thing, so it is deliberately absent here.
    "cloudflare_managed", "turnstile", "recaptcha_v2", "hcaptcha", "servicepipe",
}

FAMILY_LABEL = {
    "blocked": "Blocked by the site",
    "wall": "Behind a gate",
    "missing": "Page not there",
    "network": "Could not connect",
    "server": "Site error",
    "render": "Needed a browser",
    "skipped": "We declined it",
    "unknown": "Unclassified",
}

# Rendering order: what an operator can act on first, and what is noise last. `unknown` is last on
# purpose and is never hidden — a growing unknown bucket means this table needs a new row, and a
# grouping that quietly swallows what it does not recognise is worse than no grouping.
FAMILY_ORDER = ["blocked", "wall", "network", "render", "server", "missing", "skipped", "unknown"]


def classify(reason: str | None, status: int | None = None) -> tuple[str, bool]:
    """(family, recoverable) for one crawler reason code.

    `status` is consulted only for the `http_<n>` reasons, where the number IS the meaning. It is
    read from the code itself rather than the row's status field, because a row can carry a status
    from a redirect hop that is not the status the reason was recorded for.
    """
    r = (reason or "").strip().lower()
    if not r:
        return "unknown", False
    if r in _FAMILY:
        return _FAMILY[r], r in _RETRYABLE
    if r.startswith("http_"):
        try:
            code = int(r[5:])
        except ValueError:
            return "unknown", False
        if code in (401, 403):
            return "blocked", False          # refused, and another identical request will be too
        if code == 429:
            return "blocked", True           # refused for now — the one 4xx worth retrying
        if code == 451:
            return "wall", False             # withheld for legal reasons; not a fault to retry
        if code in (404, 410):
            return "missing", False
        if 400 <= code < 500:
            return "blocked", False
        if code >= 500:
            return "server", True            # their fault, and usually transient
        return "unknown", False
    # A SERVED_BY_* reason is a SUCCESS tag, not a failure; if one reaches this table something
    # upstream mislabelled it, and saying so beats filing it under a failure family.
    if r.startswith("served_") or r.startswith("captcha_solved"):
        return "unknown", False
    return "unknown", False


def summarise(rows) -> list[dict]:
    """Per-family counts for a list of unresolved rows, in FAMILY_ORDER, empty families dropped.

    Each row is expected to carry `reason`; `status` is optional. Rows are counted, never dropped —
    an unrecognised reason lands in `unknown` and is visible there.
    """
    agg: dict[str, dict] = {}
    for r in rows:
        fam, recoverable = classify(r.get("reason"), r.get("status"))
        a = agg.setdefault(fam, {"family": fam, "label": FAMILY_LABEL[fam], "count": 0,
                                 "recoverable": 0, "reasons": {}})
        a["count"] += 1
        a["recoverable"] += 1 if recoverable else 0
        code = (r.get("reason") or "unknown")
        a["reasons"][code] = a["reasons"].get(code, 0) + 1
    return [agg[f] for f in FAMILY_ORDER if f in agg]


def demo() -> None:
    assert classify("needs_network_path") == ("blocked", True)
    assert classify("http_404") == ("missing", False)
    assert classify("http_429") == ("blocked", True)
    assert classify("http_503") == ("server", True)
    assert classify("host_busy") == ("skipped", True), "the 617-URL bug: host_busy IS retryable"
    assert classify("skipped_gone") == ("missing", False)
    assert classify(None) == ("unknown", False)
    assert classify("something_new_2030") == ("unknown", False)
    out = summarise([{"reason": "http_403"}, {"reason": "http_403"}, {"reason": "dns"}])
    assert [x["family"] for x in out] == ["blocked", "network"], out
    assert out[0]["count"] == 2 and out[0]["reasons"] == {"http_403": 2}
    print("errorclass demo OK")


if __name__ == "__main__":
    demo()
