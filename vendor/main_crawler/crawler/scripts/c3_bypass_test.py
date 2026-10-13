#!/usr/bin/env python3
"""C3 (CamoFox) anti-bot bypass capability test — standalone harness.

Renders each target through the CamoFox stealth browser (REST API on :9377), WITH a settle wait so
JS challenges auto-solve, captures rendered HTML + aria snapshot + screenshot + final URL, and
classifies BYPASSED vs the vendor's challenge/block page. One request per target. Prints a scorecard.

Run from cralwer/:
  CAMOFOX_URL=http://127.0.0.1:9377 CAMOFOX_API_KEY=mallory-camofox-key \
  C3_SHOT_DIR=/path/to/shots python scripts/c3_bypass_test.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import httpx

BASE = os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377").rstrip("/")
KEY = os.environ.get("CAMOFOX_API_KEY", "mallory-camofox-key")
SHOT_DIR = os.environ.get("C3_SHOT_DIR", "c3_shots")
WAIT_MS = int(os.environ.get("C3_WAIT_MS", "12000"))
HDR = {"Authorization": f"Bearer {KEY}"} if KEY else {}

C3_LAB = os.environ.get("C3_LAB_URL", "http://127.0.0.1:9080")
C3_SOLVE = os.environ.get("CAMOFOX_SOLVE", "1") == "1"

# (cdn-hint, short-name, url). User-provided authorized OSINT targets — public defense/gov info sites.
# We test whether C3 retrieves the PUBLIC page vs gets a bot-block/challenge. One request each.
TARGETS = [
    ("Akamai", "army.mil", "https://www.army.mil/"),
    ("Akamai", "navy.mil", "https://www.navy.mil/"),
    ("Akamai", "af.mil", "https://www.af.mil/"),
    ("Akamai", "marines.mil", "https://www.marines.mil/"),
    ("Akamai", "defense.gov", "https://www.defense.gov/"),
    ("Akamai", "dod.gov", "https://www.dod.gov/"),
    ("Akamai", "spaceforce.mil", "https://www.spaceforce.mil/"),
    ("Fastly", "defensenews.com", "https://www.defensenews.com/"),
    ("Cloudflare", "janes.com", "https://www.janes.com/"),
    ("Cloudflare", "breakingdefense.com", "https://breakingdefense.com/"),
]

# Challenge/block signatures per vendor (lowercased substring match on html + snapshot + final_url).
SIGS = {
    "Cloudflare JS Challenge": ["just a moment", "challenge-platform", "cf-chl", "cf-browser-verification",
        "attention required", "enable javascript and cookies", "__cf_chl", "challenges.cloudflare.com",
        "checking your browser", "checking if the site connection is secure", "cf-mitigated"],
    "Cloudflare Turnstile": ["cf-turnstile", "challenges.cloudflare.com/turnstile", "turnstile"],
    "Akamai Bot Manager": ["access denied", "akamaighost", "reference #", "errors.edgesuite.net",
        "you don't have permission to access"],
    "DataDome": ["captcha-delivery.com", "geo.captcha-delivery.com", "datadome",
        "please enable js and disable any ad blocker", "interstitial.captcha"],
    "HUMAN (PerimeterX)": ["px-captcha", "perimeterx", "access to this page has been denied",
        "captcha.px", "px.gif", "human security", "please verify you are a human"],
    "Imperva": ["incapsula incident id", "_incapsula_resource", "pardon our interruption",
        "request unsuccessful", "imperva"],
}

# Titles that ALWAYS mean a block, regardless of vendor.
BLOCK_TITLES = ["just a moment", "access denied", "pardon our interruption", "attention required",
                "please verify", "you have been blocked", "access to this page has been denied",
                "checking your browser", "verify you are human", "request unsuccessful",
                "403 forbidden", "denied"]


def _flatten(node, out):
    """CamoFox aria snapshot (JSON tree of {role,name,children}) → text lines."""
    if isinstance(node, str):
        if node.strip():
            out.append(node.strip())
    elif isinstance(node, dict):
        for k in ("name", "text", "value", "label"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        for k, v in node.items():
            if k not in ("name", "text", "value", "label") and isinstance(v, (dict, list)):
                _flatten(v, out)
    elif isinstance(node, list):
        for it in node:
            _flatten(it, out)


def snapshot_text(snap):
    if isinstance(snap, str):
        return snap
    out = []
    _flatten(snap, out)
    return "\n".join(out)


def classify(title, html, snap_text, final_url):
    """Return (verdict, detected_markers, content_len). Checks block markers across ALL vendors so
    the verdict is robust regardless of which CDN the site runs. A substantial rendered page with no
    block title = BYPASSED (retrieved the public content); a thin challenge/deny page = BLOCKED."""
    blob = f"{title}\n{snap_text}\n{html}\n{final_url}".lower()
    detected = {v: sorted({s for s in sigs if s in blob}) for v, sigs in SIGS.items()}
    detected = {v: hs for v, hs in detected.items() if hs}
    title_low = (title or "").lower()
    title_block = any(t in title_low for t in BLOCK_TITLES)
    redirected_captcha = any(h in final_url.lower() for h in
                             ("captcha-delivery.com", "challenges.cloudflare.com/turnstile", "/challenge"))
    content = len(snap_text)

    if title_block or redirected_captcha:
        return ("BLOCKED", detected, content)
    if detected and content < 800:                   # block markers + thin page = active challenge
        return ("BLOCKED", detected, content)
    if content >= 1200:                              # substantial real content = public page retrieved
        return ("BYPASSED", detected, content)
    return ("INCONCLUSIVE", detected, content)


def _close_tab(client, tab, user):
    if tab:
        try:
            client.delete(f"{BASE}/tabs/{tab}", params={"userId": user}, timeout=10)
        except Exception:
            pass


def run_target(client, vendor, name, url):
    user = f"c3test-{abs(hash(name)) % 4}"
    tab = None
    rec = {"vendor": vendor, "name": name, "url": url, "verdict": "INCONCLUSIVE",
           "title": "", "final_url": url, "detected": {}, "content": 0, "note": ""}
    try:
        r = client.post(f"{BASE}/tabs", json={"userId": user, "sessionKey": "s", "url": url}, timeout=60)
        if r.status_code >= 400:
            rec["note"] = f"tab create {r.status_code}: {r.text[:120]}"
            return rec
        data = r.json()
        tab = data.get("tabId") or data.get("id")
        rec["final_url"] = data.get("url") or url
        if not tab:
            rec["note"] = "no tabId"
            return rec

        # settle — let JS challenges auto-solve
        try:
            client.post(f"{BASE}/tabs/{tab}/wait",
                        json={"userId": user, "timeout": WAIT_MS, "waitForNetwork": True}, timeout=WAIT_MS/1000 + 15)
        except Exception:
            pass

        # rendered html + title + final href in one evaluate
        html = title = ""
        try:
            expr = "JSON.stringify({h:document.documentElement.outerHTML,t:document.title,u:location.href})"
            e = client.post(f"{BASE}/tabs/{tab}/evaluate", json={"userId": user, "expression": expr},
                            headers=HDR, timeout=30)
            res = e.json().get("result")
            if isinstance(res, str):
                obj = json.loads(res)
                html, title, rec["final_url"] = obj.get("h", ""), obj.get("t", ""), obj.get("u") or rec["final_url"]
        except Exception as exc:
            rec["note"] = f"evaluate: {exc}"

        # aria snapshot (content proxy)
        snap_text = ""
        try:
            s = client.get(f"{BASE}/tabs/{tab}/snapshot", params={"userId": user}, timeout=30)
            if s.status_code < 400:
                payload = s.json() if s.headers.get("content-type", "").startswith("application/json") else {"snapshot": s.text}
                snap_text = snapshot_text(payload.get("snapshot") if isinstance(payload, dict) else payload)
        except Exception:
            pass
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = (m.group(1).strip() if m else "")[:200]

        # screenshot evidence
        try:
            sc = client.get(f"{BASE}/tabs/{tab}/screenshot", params={"userId": user, "fullPage": "false"}, timeout=30)
            if sc.status_code < 400 and sc.content:
                os.makedirs(SHOT_DIR, exist_ok=True)
                with open(os.path.join(SHOT_DIR, f"{name}.png"), "wb") as fh:
                    fh.write(sc.content)
        except Exception:
            pass

        verdict, detected, content = classify(title, html, snap_text, rec["final_url"])
        rec.update(verdict=verdict, title=title[:80], detected=detected, content=content)
        return rec
    except Exception as exc:
        rec["note"] = f"{type(exc).__name__}: {exc}"
        return rec
    finally:
        _close_tab(client, tab, user)


def test_solver(client, page_name, test_url):
    """Test CamoFox captcha detection + /solve endpoint against a C3 lab page."""
    user = f"c3test-{abs(hash(page_name)) % 4}"
    tab = None
    rec = {"page": page_name, "url": test_url, "detected": None,
           "solved": False, "token": "", "solve_time": 0.0, "note": ""}
    try:
        r = client.post(f"{BASE}/tabs", json={"userId": user, "sessionKey": "s", "url": test_url}, timeout=60)
        if r.status_code >= 400:
            rec["note"] = f"tab create {r.status_code}"
            return rec
        data = r.json()
        tab = data.get("tabId") or data.get("id")
        if not tab:
            rec["note"] = "no tabId"
            return rec

        # settle so the widget renders
        try:
            client.post(f"{BASE}/tabs/{tab}/wait",
                        json={"userId": user, "timeout": 8000, "waitForNetwork": True}, timeout=20)
        except Exception:
            pass

        # detect
        try:
            d = client.post(f"{BASE}/tabs/{tab}/captcha", json={"userId": user}, timeout=10)
            rec["detected"] = d.json()
        except Exception as exc:
            rec["note"] += f" detect_err:{exc}"

        if not C3_SOLVE:
            rec["note"] += " solve skipped (CAMOFOX_SOLVE=0)"
            return rec

        # solve
        t0 = time.time()
        captcha_type = (rec["detected"] or {}).get("captchaType") or "recaptcha"
        try:
            s = client.post(f"{BASE}/tabs/{tab}/solve",
                            json={"userId": user, "captchaType": captcha_type, "maxWaitMs": 45000},
                            timeout=60)
            res = s.json()
            rec["solved"] = bool(res.get("ok") or res.get("solved"))
            rec["token"] = (res.get("token") or "")[:40]
        except Exception as exc:
            rec["note"] += f" solve_err:{exc}"
        rec["solve_time"] = time.time() - t0
        return rec
    except Exception as exc:
        rec["note"] += f" {type(exc).__name__}:{exc}"
        return rec
    finally:
        _close_tab(client, tab, user)


def main():
    print(f"C3 bypass test → {BASE}  (settle {WAIT_MS}ms, datacenter IP, no proxy)\n")
    rows = []
    with httpx.Client(follow_redirects=True) as client:
        for vendor, name, url in TARGETS:
            t0 = time.time()
            rec = run_target(client, vendor, name, url)
            dt = time.time() - t0
            det = ",".join(rec["detected"].keys()) or "-"
            print(f"  [{rec['verdict']:12}] {vendor:11} {name:20} {int(dt):>3}s  "
                  f"content={rec['content']:<6} detected={det:<20} title={rec['title']!r} {rec['note']}")
            rows.append(rec)

    print("\n===== SCORECARD (per URL) =====")
    ok = sum(1 for r in rows if r["verdict"] == "BYPASSED")
    for r in rows:
        print(f"  {r['verdict']:12} {r['vendor']:11} {r['name']}")
    print(f"\n  C3 RETRIEVED {ok} / {len(rows)} sites (datacenter IP, no proxy)")

    print("\n  by CDN:")
    for cdn in ("Akamai", "Fastly", "Cloudflare"):
        cr = [r for r in rows if r["vendor"] == cdn]
        if cr:
            got = sum(1 for r in cr if r["verdict"] == "BYPASSED")
            print(f"    {cdn:11} {got}/{len(cr)} retrieved")

    # Solver testbed section
    print(f"\n===== SOLVER TESTBED ({C3_LAB}) =====")
    solver_rows = []
    with httpx.Client(follow_redirects=True) as client:
        for page_name, test_url in (("recaptcha", f"{C3_LAB}/recaptcha"),
                                    ("hcaptcha", f"{C3_LAB}/hcaptcha"),
                                    ("turnstile", f"{C3_LAB}/turnstile"),
                                    ("datadome", f"{C3_LAB}/datadome")):
            sr = test_solver(client, page_name, test_url)
            det = sr["detected"].get("captchaType") if sr["detected"] else "-"
            print(f"  {page_name:10} detected={det:12} solved={sr['solved']} time={sr['solve_time']:.1f}s "
                  f"token={sr['token'][:20]}... {sr['note']}")
            solver_rows.append(sr)
    solved_ok = sum(1 for s in solver_rows if s["solved"])
    print(f"  Solver passed {solved_ok}/{len(solver_rows)}")

    os.makedirs(SHOT_DIR, exist_ok=True)
    with open(os.path.join(SHOT_DIR, "results.json"), "w", encoding="utf-8") as fh:
        json.dump({"bypass": rows, "solver": solver_rows}, fh, indent=2)
    print(f"\n  screenshots + results.json → {SHOT_DIR}")


if __name__ == "__main__":
    sys.exit(main())
