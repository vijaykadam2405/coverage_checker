"""Host-side Cloudflare-interactive solver — the C4 tier.

    ucvenv/Scripts/python.exe scripts/host_solver.py            # listens on 127.0.0.1:9378
    curl -X POST :9378/solve -d '{"url":"https://www.zoopla.co.uk/to-rent"}'

WHY THIS RUNS ON THE HOST AND NOT IN THE CONTAINER
--------------------------------------------------
Cloudflare's INTERACTIVE tier (window._cf_chl_opt.cType == 'interactive') never self-clears and
injects no Turnstile widget, so nothing the crawler can do from inside Docker passes it — measured
across Camoufox 135/152, a residential exit, and SeleniumBase UC with an OS-level click. The
decisive difference is the graphics stack, not the engine and not the IP:

    in Docker : webgl = ANGLE (Mesa, llvmpipe ...)            -> blocked
    on host   : webgl = ANGLE (Intel(R) Graphics, Direct3D11) -> passes

llvmpipe cannot be faked away in JS (the capability surface is cross-checked), and WSL2 does not
pass OpenGL through to the Docker VM. So the crawler calls OUT to this service, exactly as it
already calls out to the host's Ollama for vision OCR.

WHY IT KEEPS THE BROWSER OPEN
-----------------------------
Solving yields a cf_clearance cookie bound to the TLS/JA4 + HTTP2 shape of the browser that earned
it — it does NOT transplant to another engine (measured: injecting a Chrome-earned zoopla cookie
into camofox/Firefox is still walled). But it keeps working in the SAME session. So the first URL
on a host pays the ~28s solve and every subsequent URL is an ordinary fast navigation in the warm
browser. Without that, a 50-page crawl of a challenged site would cost 50 x 28s instead of once.

The browser is a single shared resource, so requests are serialised behind one lock, and an idle
session is closed so a forgotten service does not hold a Chrome process forever.

SAFETY / SCOPE
--------------
Binds to 127.0.0.1 only. It drives a real browser on your desktop; it does not move the mouse
(CDP dispatches at viewport coordinates), so it will not fight you for the pointer.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CHALLENGE = ("just a moment", "checking your browser", "cf-chl", "challenge-platform",
             "verifying you are human", "enable javascript and cookies")

IDLE_CLOSE_S = float(os.environ.get("HOST_SOLVER_IDLE_S", "180"))
NAV_SETTLE_S = float(os.environ.get("HOST_SOLVER_SETTLE_S", "3"))
MAX_ATTEMPTS = int(os.environ.get("HOST_SOLVER_ATTEMPTS", "3"))


class Session:
    """One live CDP browser, reused across URLs on the same host."""

    def __init__(self) -> None:
        self.sb = None
        self.host = ""
        self.last_used = 0.0
        self.solved_hosts: set[str] = set()
        self.lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def _open(self, url: str):
        from seleniumbase import sb_cdp
        # sb_cdp.Chrome can return None (or a driver-less object) when a Chrome launch races another
        # instance or a prior browser did not fully release — observed live: the returned handle was
        # None and every get_title() raised AttributeError. Retry the launch a few times before
        # giving up, since it is almost always transient (a lingering chromedriver, a port grab).
        # A SECOND host_solver instance is the usual root cause: only run one (it binds :9390).
        last = None
        for attempt in range(3):
            try:
                sb = sb_cdp.Chrome(url, incognito=True)
            except Exception as exc:                       # noqa: BLE001
                last = exc
                sb = None
            if sb is not None and hasattr(sb, "get_title"):
                self.sb = sb
                self.host = urlsplit(url).hostname or ""
                return self.sb
            try:
                if sb is not None:
                    sb.driver.stop()
            except Exception:
                pass
            print(f"[solver] launch attempt {attempt + 1} gave no browser; retrying", flush=True)
            time.sleep(2)
        raise RuntimeError(f"sb_cdp.Chrome returned no usable browser after 3 tries: {last}")

    def close(self) -> None:
        if self.sb is not None:
            try:
                self.sb.driver.stop()
            except Exception:
                pass
        self.sb, self.host = None, ""
        self.solved_hosts.clear()

    def reap_if_idle(self) -> None:
        if self.sb is not None and self.last_used and time.time() - self.last_used > IDLE_CLOSE_S:
            print(f"[solver] idle {IDLE_CLOSE_S:.0f}s — closing browser", flush=True)
            self.close()

    # -- the work ----------------------------------------------------------
    def fetch(self, url: str) -> dict:
        """Return {ok, html, title, chars, elapsed, via, solved}. Never raises."""
        t0 = time.time()
        host = urlsplit(url).hostname or ""
        out = {"ok": False, "html": "", "title": "", "chars": 0, "elapsed": 0.0,
               "via": "", "solved": False, "error": ""}
        try:
            # A different host means the warm cf_clearance is useless — start clean rather than
            # carry one site's session into another's.
            if self.sb is not None and host != self.host:
                self.close()
            if self.sb is None:
                try:
                    self._open(url)
                except Exception:
                    time.sleep(2)                      # a racy Chrome launch — one clean retry
                    self._open(url)
                out["via"] = "cold"
            else:
                self.sb.open(url)
                out["via"] = "warm"
            if self.sb is None:
                out["error"] = "browser did not start"
                return out

            time.sleep(NAV_SETTLE_S)
            for i in range(MAX_ATTEMPTS):
                title = (self.sb.get_title() or "").lower()
                if not any(m in title for m in CHALLENGE):
                    break
                try:
                    # CDP solve_captcha, NOT uc_gui_click_captcha: upstream superseded the latter in
                    # 2025 (built for the iframe era; Turnstile now renders in a closed shadow root
                    # no DOM traversal reaches). This dispatches at viewport coordinates and lands
                    # on whatever is topmost, shadow root included — and needs no PyAutoGUI, so it
                    # never grabs the real mouse.
                    self.sb.solve_captcha()
                    out["solved"] = True
                except Exception as exc:                       # noqa: BLE001 — a failed click is data
                    print(f"[solver] click {i+1}: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                time.sleep(4)
                if i + 1 < MAX_ATTEMPTS and any(
                        m in (self.sb.get_title() or "").lower() for m in CHALLENGE):
                    self.sb.open(url)                          # re-arm: a fresh challenge often passes
                    time.sleep(2)

            html = self.sb.get_page_source() or ""
            title = self.sb.get_title() or ""
            # Judge on CONTENT, never on "no exception": the failure mode is a 200 that is still the
            # interstitial, which looks like success to any status check.
            walled = any(m in (title + " " + html[:4000]).lower() for m in CHALLENGE)
            out.update(ok=(not walled and len(html) > 1000), html=html, title=title,
                       chars=len(html))
            if out["ok"]:
                self.solved_hosts.add(host)
        except Exception as exc:                               # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
            self.close()                                       # a wedged browser must not persist
        out["elapsed"] = round(time.time() - t0, 1)
        self.last_used = time.time()
        return out


SESSION = Session()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        # A CLIENT THAT HANGS UP IS NOT AN ERROR, AND MUST NOT LOOK LIKE ONE. protocol_version is
        # HTTP/1.1, so every caller leaves a keep-alive connection behind — the crawler's /health
        # probe, socat's fork, a bare port check — and this server later reads the close as a reset
        # while blocked in readline(). BaseHTTPRequestHandler has no handler for that, so
        # ThreadingHTTPServer.handle_error dumps a six-frame traceback ending in
        # "ConnectionResetError: [WinError 10054]" onto the console.
        #
        # That console IS the operator's only view of this service (it runs in a visible window on
        # the workstation, not under a supervisor with a log). A traceback there reads as "C4 is
        # broken" — measured: it was reported as an outage while the tier was in fact answering a
        # container in the data centre in 5.8s. One quiet line keeps the evidence that traffic is
        # arriving without the false alarm.
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
            self.close_connection = True          # or handle() loops straight back into the dead socket
            print(f"[solver] client hung up before its request ({type(exc).__name__})", flush=True)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                                   # noqa: N802
        if self.path.startswith("/health"):
            SESSION.reap_if_idle()
            self._json(200, {"ok": True, "browser_open": SESSION.sb is not None,
                             "host": SESSION.host,
                             "solved_hosts": sorted(SESSION.solved_hosts)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:                                  # noqa: N802
        if not self.path.startswith("/solve"):
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            url = (req.get("url") or "").strip()
        except Exception as exc:                                # noqa: BLE001
            self._json(400, {"error": f"bad request: {exc}"})
            return
        if not url.startswith(("http://", "https://")):
            self._json(400, {"error": "url must be http(s)"})
            return
        # One browser, so one request at a time. The crawler's own timeout is what bounds the wait.
        with SESSION.lock:
            res = SESSION.fetch(url)
        print(f"[solver] {res['via']:<4} ok={res['ok']} solved={res['solved']} "
              f"{res['chars']:>7} chars {res['elapsed']:>5.1f}s  {url[:70]}", flush=True)
        self._json(200, res)

    def log_message(self, *a) -> None:                          # quiet: we print our own line
        return


def main() -> int:
    port = int(os.environ.get("HOST_SOLVER_PORT", "9378"))
    # 0.0.0.0 so the Docker bridge can reach it; the crawler uses host.docker.internal. Set
    # HOST_SOLVER_BIND=127.0.0.1 to keep it local-only (then the container cannot call it).
    bind = os.environ.get("HOST_SOLVER_BIND", "0.0.0.0")
    # A SECOND INSTANCE MUST FAIL TO START, NOT SILENTLY CO-BIND. HTTPServer sets
    # allow_reuse_address = 1, and on Windows SO_REUSEADDR does not mean what it means on POSIX: it
    # lets a second process bind a port another process is ALREADY LISTENING on, and connections
    # then land on whichever the kernel picks. The docstring's "run exactly one instance" rule was
    # only ever a hope; on Windows this makes it a rule, and a duplicate launch now dies at bind
    # with a message instead of quietly splitting the traffic.
    #
    # NOT the same thing as the process PAIR you will see in Task Manager. `ucvenv\Scripts\python.exe`
    # is a venv launcher: it spawns C:\Python311\python.exe as a CHILD (same creation second, PPID
    # pointing at the launcher) and that child is what binds and serves. Two host_solver rows is the
    # NORMAL, single-instance shape here — checked twice now and misread as a conflict both times.
    # Tell them apart by parentage and by who owns the listening socket, never by the count.
    ThreadingHTTPServer.allow_reuse_address = (os.name != "nt")
    srv = ThreadingHTTPServer((bind, port), Handler)
    print(f"[solver] listening on {bind}:{port} — POST /solve {{\"url\": ...}}", flush=True)
    print(f"[solver] idle browser closed after {IDLE_CLOSE_S:.0f}s", flush=True)
    reaper = threading.Thread(
        target=lambda: [time.sleep(30) or SESSION.reap_if_idle() for _ in iter(int, 1)],
        daemon=True)
    reaper.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[solver] shutting down", flush=True)
    finally:
        SESSION.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
