#!/usr/bin/env python3
"""C3 detection-lab runner — measures the CamoFox stealth browser against our self-hosted lab
(c3_testbed) + optional public panels, so we can harden C3 and track the delta after each change.

Per target: POST /tabs (navigate) -> POST /wait (settle, so async probes finish) -> [drive trusted
input for the behavior gate] -> POST /evaluate to read <pre id="c3result"> -> classify PASS/FAIL.

Run from cralwer/ (CamoFox on the host, testbed reachable from the CamoFox container):
  CAMOFOX_URL=http://127.0.0.1:9377 CAMOFOX_API_KEY=mallory-camofox-key \
  C3_TESTBED_URL=http://host.docker.internal:9085 python scripts/c3_lab.py
Add C3_LAB_PANELS=1 to also hit purpose-built public panels (sannysoft/creepjs/tls.peet.ws).
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

CAMOFOX = os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377").rstrip("/")
KEY = os.environ.get("CAMOFOX_API_KEY", "mallory-camofox-key")
TESTBED = os.environ.get("C3_TESTBED_URL", "http://host.docker.internal:9085").rstrip("/")
WAIT_MS = int(os.environ.get("C3_WAIT_MS", "6000"))
HDR = {"Authorization": f"Bearer {KEY}"} if KEY else {}

# (key, url, needs_behavioral_drive)
LAB = [
    ("fp", f"{TESTBED}/fp", False),
    ("challenge", f"{TESTBED}/challenge", False),
    ("behavior", f"{TESTBED}/behavior", True),
    ("locale", f"{TESTBED}/locale-check", False),
]
PANELS = [
    ("sannysoft", "https://bot.sannysoft.com/", False),
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/", False),
    ("tls-peet", "https://tls.peet.ws/api/all", False),
]


def _new_tab(client, user, url):
    r = client.post(f"{CAMOFOX}/tabs", json={"userId": user, "sessionKey": "s", "url": url}, timeout=60)
    if r.status_code >= 400:
        return None, f"tab {r.status_code}: {r.text[:100]}"
    return (r.json().get("tabId") or r.json().get("id")), None


def _wait(client, user, tab, ms):
    try:
        client.post(f"{CAMOFOX}/tabs/{tab}/wait",
                    json={"userId": user, "timeout": ms, "waitForNetwork": True}, timeout=ms / 1000 + 15)
    except Exception:
        pass


def _drive_behavior(client, user, tab):
    """Generate TRUSTED mouse+scroll events so the behavioral gate reveals (previews the render()
    behavioral pre-capture). CamoFox scroll = real mouse.wheel; a selector click drives the humanized
    cursor along a curved path -> many trusted mousemove events (coordinates aren't accepted by /click)."""
    def _post(path, body):
        try:
            client.post(f"{CAMOFOX}/tabs/{tab}/{path}", json={"userId": user, **body}, timeout=15)
        except Exception:
            pass
    _post("scroll", {"direction": "down", "amount": 500})
    _post("scroll", {"direction": "down", "amount": 500})
    for sel in ("h3", "pre", "body", "div"):
        _post("click", {"selector": sel})
    _post("scroll", {"direction": "up", "amount": 300})


def _read_result(client, user, tab):
    """Return the parsed #c3result JSON (our self-hosted verdict), or the raw page text for panels."""
    try:
        expr = "(document.getElementById('c3result')||{}).textContent || document.title"
        e = client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", json={"userId": user, "expression": expr},
                        headers=HDR, timeout=30)
        res = e.json().get("result")
        if isinstance(res, str) and res.strip().startswith("{"):
            return json.loads(res), res
        return None, (res or "")
    except Exception as exc:
        return None, f"evaluate_err: {exc}"


def classify(key, verdict, raw):
    if verdict is None:
        return "INCONCLUSIVE", raw[:80]
    if key == "fp":
        n = verdict.get("bot_score", 99)
        leaks = verdict.get("leaks", [])
        return ("PASS" if n == 0 else "LEAK", f"bot_score={n} {leaks}")
    if key == "challenge":
        return ("PASS" if verdict.get("passed") else "FAIL", f"token={verdict.get('token','')}")
    if key == "behavior":
        return ("PASS" if verdict.get("passed") else "FAIL",
                f"moves={verdict.get('moves')} scrolled={verdict.get('scrolled')} trusted={verdict.get('allTrusted')}")
    if key == "locale":
        return ("PASS" if verdict.get("consistent") else "MISMATCH",
                f"tz={verdict.get('tz')} lang={verdict.get('language')} al={verdict.get('accept_language')}")
    return ("?", str(verdict)[:80])


def run_one(client, key, url, drive):
    user = f"c3lab-{abs(hash(key)) % 4}"
    tab, err = _new_tab(client, user, url)
    if err:
        return key, "INCONCLUSIVE", err
    try:
        _wait(client, user, tab, WAIT_MS)
        if drive:
            _drive_behavior(client, user, tab)
            _wait(client, user, tab, 1500)
        verdict, raw = _read_result(client, user, tab)
        v, detail = classify(key, verdict, raw)
        return key, v, detail
    finally:
        try:
            client.delete(f"{CAMOFOX}/tabs/{tab}", params={"userId": user}, timeout=10)
        except Exception:
            pass


def main():
    targets = list(LAB)
    if os.environ.get("C3_LAB_PANELS") == "1":
        targets += PANELS
    print(f"C3 lab -> CamoFox {CAMOFOX}, testbed {TESTBED} (wait {WAIT_MS}ms)\n")
    rows = []
    with httpx.Client(follow_redirects=True) as client:
        for key, url, drive in targets:
            t0 = time.time()
            k, v, detail = run_one(client, key, url, drive)
            print(f"  [{v:12}] {k:12} {int(time.time()-t0):>3}s  {detail}")
            rows.append((k, v, detail))
    npass = sum(1 for _, v, _ in rows if v == "PASS")
    print(f"\n  C3 lab: {npass}/{len(rows)} PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
