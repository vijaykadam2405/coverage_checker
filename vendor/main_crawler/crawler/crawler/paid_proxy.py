"""Access control for the PAID (Bright Data) proxy egress.

Every request through this path costs real money — residential bandwidth is billed per GB, so a bug
here is a bill, not a stack trace. The gate therefore FAILS CLOSED at every step:

  disabled by default  ·  empty allowlist grants nothing  ·  caps are hard, not advisory
  ·  a corrupt/unreadable usage file denies rather than resets the counter

MVP intent (2026-07): free tiers first for everything. Only the handful of sites that no free tier
can reach get paid egress, and only after those tiers have actually been tried on that URL. The
allowlist is the budget: adding a host is the decision to spend on it.

Wiring: CamoFox takes its proxy from container env, so paid egress is a SECOND CamoFox instance
(compose service `camofox-paid`, profile `paid`). This module never proxies anything itself — it
only answers "may this URL use the paid instance, and what is its URL?", so every spend decision
sits in one auditable place.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from urllib.parse import urlsplit

from . import config

log = logging.getLogger("paid_proxy")

_LOCK = threading.Lock()
# Under config.DATA_DIR, so CRAWLER_DATA_DIR alone isolates an instance. The old absolute default
# meant two crawlers with separate data directories still shared this file — and the spend counter
# is a read-modify-write full overwrite, so sharing it does not just merge the counts, it LOSES
# them: both read N, both write N+1, and the daily cap is silently exceeded. _LOCK is a thread
# lock and does nothing across processes.
_USAGE_PATH = os.environ.get("PAID_PROXY_USAGE_FILE") or str(config.DATA_DIR / "paid_proxy_usage.json")


def enabled() -> bool:
    """Master switch. Default OFF: paid egress is never a side effect of deploying."""
    return os.environ.get("BRIGHTDATA_ENABLED", "0") == "1"


def base_url() -> str:
    return os.environ.get("CAMOFOX_PAID_URL", "http://camofox-paid:9377").rstrip("/")


def allowed_hosts() -> set[str]:
    """Hosts permitted to spend. Empty means empty — an unset allowlist grants nothing.

    Matching is exact-or-subdomain: "sipri.org" covers "www.sipri.org", but never "notsipri.org".
    """
    raw = os.environ.get("BRIGHTDATA_HOSTS", "")
    return {h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()}


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def host_allowed(url: str) -> bool:
    """Exact-or-subdomain match, or "*" for any host.

    "*" is deliberate and load-bearing: the operator wants ANY site the free tiers cannot serve to
    escalate automatically, rather than pre-naming every domain. It removes the per-host budget, so
    the DAILY CAP becomes the only spend ceiling — keep it low. An empty list still grants nothing;
    the wildcard has to be asked for explicitly.
    """
    host = _host_of(url)
    if not host:
        return False
    allowed = allowed_hosts()
    if "*" in allowed:
        return True
    for a in allowed:
        if host == a or host.endswith("." + a):
            return True
    return False


def _today() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y-%m-%d")


def _read_usage() -> dict:
    """Usage counters, persisted so the daily cap survives a restart.

    A missing file is a genuine fresh start. An unreadable one is NOT: returning zeros there would
    let a corrupt file silently reset the budget every run, which is the failure that costs money.
    """
    try:
        with open(_USAGE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"date": _today(), "requests": 0}
    except Exception as exc:                       # noqa: BLE001
        log.error("paid-proxy usage file unreadable (%s) — denying until fixed", exc)
        return {"date": _today(), "requests": -1}  # sentinel: deny
    if data.get("date") != _today():
        return {"date": _today(), "requests": 0}   # new UTC day, fresh daily budget
    return data


def _write_usage(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_USAGE_PATH), exist_ok=True)
        with open(_USAGE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception as exc:                       # noqa: BLE001
        # Can't persist => can't enforce the cap across restarts. Say so loudly.
        log.error("cannot persist paid-proxy usage (%s) — the daily cap is NOT enforced", exc)


def daily_cap() -> int:
    return int(os.environ.get("BRIGHTDATA_DAILY_REQUESTS", "500"))


def usage_today() -> int:
    return max(0, _read_usage().get("requests", 0))


def remaining_today() -> int:
    used = _read_usage().get("requests", 0)
    if used < 0:                                   # unreadable counter => no budget
        return 0
    return max(0, daily_cap() - used)


def allow(url: str, *, free_tiers_tried: bool = False) -> bool:
    """The single decision point: may this URL use paid egress right now?

    `free_tiers_tried` is required to be True by the caller's own honesty — paid egress is a LAST
    resort, not a shortcut. Passing False is how you assert "I haven't tried the free path yet",
    and the gate refuses.
    """
    if not enabled():
        return False
    if not free_tiers_tried:
        log.debug("paid proxy refused for %s: free tiers not tried yet", url)
        return False
    if not host_allowed(url):
        return False
    if remaining_today() <= 0:
        log.warning("paid proxy DAILY CAP reached (%d) — refusing %s", daily_cap(), _host_of(url))
        return False
    return True


def note_use(url: str, n: int = 1) -> int:
    """Record n paid requests. Returns the new total. Call ONLY when the request actually went out."""
    with _LOCK:
        data = _read_usage()
        used = max(0, data.get("requests", 0)) + n
        _write_usage({"date": _today(), "requests": used})
    log.info("paid proxy used for %s (%d/%d today)", _host_of(url), used, daily_cap())
    return used


def status() -> dict:
    """For the dashboard / a run report: what is switched on and how much has been spent."""
    return {
        "enabled": enabled(),
        "hosts": sorted(allowed_hosts()),
        "daily_cap": daily_cap(),
        "used_today": usage_today(),
        "remaining_today": remaining_today(),
        "endpoint": base_url() if enabled() else None,
    }


def demo() -> None:
    """Self-check: the gate must fail closed on every axis."""
    import tempfile

    os.environ["PAID_PROXY_USAGE_FILE"] = os.path.join(tempfile.mkdtemp(), "usage.json")
    global _USAGE_PATH
    _USAGE_PATH = os.environ["PAID_PROXY_USAGE_FILE"]

    os.environ.pop("BRIGHTDATA_ENABLED", None)
    os.environ["BRIGHTDATA_HOSTS"] = "sipri.org"
    assert not allow("https://www.sipri.org/", free_tiers_tried=True), "spent while disabled"

    os.environ["BRIGHTDATA_ENABLED"] = "1"
    assert not allow("https://www.sipri.org/", free_tiers_tried=False), "spent before trying free tiers"
    assert allow("https://www.sipri.org/", free_tiers_tried=True), "allowlisted host refused"
    assert allow("https://sipri.org/x", free_tiers_tried=True), "bare domain refused"
    assert not allow("https://notsipri.org/", free_tiers_tried=True), "suffix confusion — would spend on a lookalike"
    assert not allow("https://example.com/", free_tiers_tried=True), "spent on a host not on the list"

    os.environ["BRIGHTDATA_HOSTS"] = ""
    assert not allow("https://www.sipri.org/", free_tiers_tried=True), "empty allowlist granted access"

    os.environ["BRIGHTDATA_HOSTS"] = "sipri.org"
    os.environ["BRIGHTDATA_DAILY_REQUESTS"] = "2"
    note_use("https://www.sipri.org/"); note_use("https://www.sipri.org/")
    assert not allow("https://www.sipri.org/", free_tiers_tried=True), "spent past the daily cap"
    assert remaining_today() == 0

    with open(_USAGE_PATH, "w", encoding="utf-8") as fh:
        fh.write("{corrupt")
    assert remaining_today() == 0, "corrupt usage file reset the budget instead of denying"

    print("all paid-proxy access-control self-checks passed")


if __name__ == "__main__":
    demo()
