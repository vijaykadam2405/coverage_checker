"""Harvest a Cloudflare `cf_clearance` cookie on the HOST and inject it into the C3 browser.

    python scripts/cf_clearance.py harvest https://www.zoopla.co.uk/to-rent
    python scripts/cf_clearance.py inject  --user crawler-0
    python scripts/cf_clearance.py status

WHY THIS EXISTS
---------------
Some sites do not present a solvable challenge at all. Measured on zoopla.co.uk/to-rent, both in
Docker and on the host with a real GPU and a real OS-level mouse click:

    iframe count: 0        <- the Turnstile widget is NEVER injected
    title: "Just a moment..."

Cloudflare hard-blocks BEFORE offering a widget, so there is nothing to click. No click technique,
no fingerprint fix and no paid solver can help: a solver needs a sitekey from a widget that was
never created, and a managed-challenge token is session-bound anyway. The verdict is about the
CLIENT, not the puzzle.

What still works is borrowing trust from a browser Cloudflare already accepts. `cf_clearance` is
the cookie it hands out once a client has passed; presenting it skips the interstitial entirely.

BINDING — why a transplant can fail
-----------------------------------
`cf_clearance` is validated against the IP and the User-Agent that earned it (and Cloudflare also
ties it to the TLS/JA3 shape). So:

  * harvest and crawl must egress from the SAME public IP — do not enable the C3 proxy for a host
    that you are transplanting into (see .env: CAMOFOX_PROXY_*), or the cookie is refused;
  * the UA must match, which is why `inject` also writes the harvested UA into the session;
  * it EXPIRES (typically ~30 min to a few hours). This is a door-opener for a focused crawl of a
    stubborn host, not a permanent bypass.

ponytail: no daemon, no scheduler, no cookie DB — one file on disk, two verbs. Re-run `harvest`
when the cookie dies.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

STORE = Path(os.environ.get("CF_CLEARANCE_STORE",
                            Path(__file__).resolve().parent / "cf_clearance.json"))
CAMOFOX_URL = os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377").rstrip("/")
CAMOFOX_KEY = os.environ.get("CAMOFOX_API_KEY", "mallory-camofox-key")


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    STORE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def harvest(url: str, attempts: int = 2) -> int:
    """Open `url` in a REAL headful Chrome on this host and keep the cf_clearance it earns.

    Uses SeleniumBase CDP Mode's `solve_captcha()`. NOT `uc_gui_click_captcha()`, which the
    SeleniumBase maintainer superseded in 2025: it was written when Turnstile lived in an iframe,
    and Turnstile has since moved into a closed shadow root, so DOM traversal cannot reach it.
    solve_captcha() instead locates the OUTER light-DOM container, reads its box model and
    dispatches the click at viewport coordinates — which lands on whatever is topmost, closed
    shadow root included. It needs no PyAutoGUI, so it does not seize the real mouse pointer.

    MEASURED 2026-07-24 on zoopla.co.uk/to-rent, a cType='interactive' challenge that nothing else
    here could touch (Camoufox coordinate click, and the old uc_gui_click_captcha on a real GPU,
    both left the checkbox inert): "Just a moment..." -> "Houses & flats to rent ... Zoopla",
    464,358 chars of real listings, from the same datacenter IP that fails everything else.

    Do NOT import selenium.webdriver anywhere in this process — it re-attaches WebDriver and is
    itself detectable (SeleniumBase docs).
    """
    from seleniumbase import sb_cdp  # imported late: only the host venv has it

    host = (urlsplit(url).hostname or "").lower()
    got, ua, title = None, "", ""
    sb = sb_cdp.Chrome(url, incognito=True)
    try:
        for attempt in range(1, attempts + 1):
            time.sleep(4)
            try:
                sb.solve_captcha()
            except Exception as exc:
                print(f"  attempt {attempt}: solve_captcha raised {type(exc).__name__}",
                      file=sys.stderr)
            time.sleep(6)
            title = sb.get_title()
            # sb_cdp exposes get_all_cookies(), not get_cookies() (that is the WebDriver API).
            cookies = {getattr(c, "name", ""): c for c in (sb.get_all_cookies() or [])}
            if "cf_clearance" in cookies:
                c = cookies["cf_clearance"]
                got = {"name": "cf_clearance", "value": getattr(c, "value", ""),
                       "domain": getattr(c, "domain", None) or f".{host}",
                       "path": getattr(c, "path", None) or "/"}
                break
            print(f"  attempt {attempt}: no cf_clearance yet (title={title!r})", file=sys.stderr)
            if attempt < attempts:
                sb.open(url)
        ua = sb.get_user_agent()
    finally:
        try:
            sb.driver.stop()
        except Exception:
            pass

    if not got:
        print(json.dumps({"ok": False, "host": host, "title": title,
                          "reason": "no cf_clearance issued — Cloudflare did not pass this client"},
                         indent=1))
        return 1

    data = _load()
    data[host] = {"cookie": got, "user_agent": ua,
                  "harvested_at": time.time(), "title": title}
    _save(data)
    print(json.dumps({"ok": True, "host": host, "title": title,
                      "user_agent": ua, "stored": str(STORE)}, indent=1))
    return 0


def inject(user: str, host: str | None = None) -> int:
    """Push every stored clearance cookie into a CamoFox session (POST /sessions/:userId/cookies)."""
    import httpx

    data = _load()
    items = {host: data[host]} if host and host in data else data
    if not items:
        print("nothing stored — run `harvest <url>` first", file=sys.stderr)
        return 1

    cookies, uas = [], set()
    for h, rec in items.items():
        c = rec["cookie"]
        cookies.append({"name": c["name"], "value": c["value"],
                        "domain": c.get("domain") or f".{h}", "path": c.get("path") or "/",
                        "secure": True, "httpOnly": True, "sameSite": "None"})
        uas.add(rec.get("user_agent") or "")

    r = httpx.post(f"{CAMOFOX_URL}/sessions/{user}/cookies", json={"cookies": cookies},
                   headers={"Authorization": f"Bearer {CAMOFOX_KEY}"}, timeout=30.0)
    ok = r.status_code < 400
    print(json.dumps({"ok": ok, "status": r.status_code, "injected": len(cookies),
                      "hosts": sorted(items), "user": user,
                      "note": "set the SAME User-Agent on the session or the cookie is refused",
                      "user_agents": sorted(u for u in uas if u),
                      "body": r.text[:200]}, indent=1))
    return 0 if ok else 1


def put(host: str, value: str, ua: str) -> int:
    """Store a cf_clearance you copied out of your OWN browser, for a host this machine cannot earn.

    Some sites never issue a clearance to an automated client at all — zoopla.co.uk hard-blocks
    before any widget is injected, even for a real headful Chrome on a real GPU on this IP. But a
    browser you actually use has history, prior clearances and a warm profile, and Cloudflare does
    pass it. This takes that cookie without anything reading your profile:

      DevTools (F12) -> Application -> Cookies -> the site -> copy the `cf_clearance` value
      DevTools Console -> `navigator.userAgent` -> copy that too (the cookie is UA-bound)

        python scripts/cf_clearance.py put --host www.zoopla.co.uk --value <paste> --ua "<paste>"
        python scripts/cf_clearance.py inject --user crawler-0

    Must be the same public IP the browser used, or Cloudflare refuses it.
    """
    host = host.lower().removeprefix("www.") if host.startswith("www.") else host.lower()
    data = _load()
    data[host] = {"cookie": {"name": "cf_clearance", "value": value,
                             "domain": f".{host}", "path": "/"},
                  "user_agent": ua, "harvested_at": time.time(), "title": "(pasted from browser)"}
    _save(data)
    print(json.dumps({"ok": True, "host": host, "stored": str(STORE),
                      "next": f"python scripts/cf_clearance.py inject --user crawler-0"}, indent=1))
    return 0


def status() -> int:
    data = _load()
    if not data:
        print("no clearances stored")
        return 0
    now = time.time()
    for h, rec in sorted(data.items()):
        age = (now - float(rec.get("harvested_at") or 0)) / 60.0
        print(f"  {h:32} age {age:6.1f} min   ua={str(rec.get('user_agent'))[:48]}")
    print("\ncf_clearance is IP + UA bound and typically dies within ~30-120 min; re-harvest when stale.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harvest"); h.add_argument("url"); h.add_argument("--attempts", type=int, default=2)
    i = sub.add_parser("inject");  i.add_argument("--user", default="crawler-0"); i.add_argument("--host")
    p = sub.add_parser("put")
    p.add_argument("--host", required=True); p.add_argument("--value", required=True)
    p.add_argument("--ua", required=True)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "harvest":
        return harvest(a.url, a.attempts)
    if a.cmd == "inject":
        return inject(a.user, a.host)
    if a.cmd == "put":
        return put(a.host, a.value, a.ua)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
