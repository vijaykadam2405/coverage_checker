"""The C4 solver's console must not report a healthy service as broken.

MEASURED 2026-08-16. The solver's window showed this, and C4 was reported as down:

    Exception occurred during processing of request from ('127.0.0.1', 50749)
    Traceback (most recent call last):
      ...
      File "C:\\Python311\\Lib\\socket.py", line 706, in readinto
        return self._sock.recv_into(b)
    ConnectionResetError: [WinError 10054] An existing connection was forcibly closed

C4 was NOT down: a container in the data centre solved baesystems.com through the relay in 5.8s
while that traceback was on screen. The stack dump is what a keep-alive client hanging up looks
like — protocol_version is HTTP/1.1, so every caller leaves a connection open and this server later
reads its close while blocked in readline(). BaseHTTPRequestHandler has no handler for that, so
ThreadingHTTPServer.handle_error dumps six frames onto the console.

That console is the operator's ONLY view of this service (a visible window on the workstation, no
supervisor, no log file), so a traceback there is not cosmetic — it is a false outage report.

The second half pins the co-bind rule. On Windows SO_REUSEADDR lets a second process bind a port
another is already LISTENING on, so "run exactly one instance" — a rule the module docstring has
carried for weeks — was unenforceable, and a duplicate would have split traffic between two Chromes
with no error anywhere.

It is worth knowing what a duplicate does NOT look like: `ucvenv\\Scripts\\python.exe` is a venv
launcher that spawns C:\\Python311\\python.exe as a child, so a healthy single solver shows TWO
host_solver processes. That pair was read as a conflict twice during this diagnosis. Parentage and
the owner of the listening socket are the discriminator; the process count is not.

Runnable directly:  python tests/test_host_solver_survives_a_dropped_client.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import struct
import threading
import time
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "host_solver.py"
_spec = importlib.util.spec_from_file_location("host_solver_under_test", _SRC)
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)          # safe to import: seleniumbase is imported lazily inside _open


def _serve(handler=None):
    """A real server on an ephemeral port. Returns (srv, port, stop)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler or hs.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port, lambda: (srv.shutdown(), srv.server_close())


def _abort(port: int) -> None:
    """Connect and RESET — the actual signature. A graceful close is NOT this bug: readline() then
    returns b'' and BaseHTTPRequestHandler shuts the connection down quietly, so a test built on
    close() alone passes with or without the fix. This one wrote that vacuous test first; SO_LINGER
    with a zero timeout is what makes close() emit an RST, which is what a dropped ssh-tunnel hop
    and an abandoned health probe look like on the wire."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    s.close()


def t_the_reset_really_does_dump_a_traceback_without_the_fix() -> None:
    """Pins the reproduction itself against a stock handler. Without this, the two tests below could
    silently stop exercising anything the day the OS or CPython changes how an RST surfaces, and
    they would keep passing — which is the failure this suite already committed once."""
    class _Bare(hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a) -> None:
            return

    srv, port, stop = _serve(_Bare)
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            for _ in range(3):
                _abort(port)
            time.sleep(0.7)
    finally:
        stop()
    assert "ConnectionResetError" in err.getvalue() or "ConnectionAbortedError" in err.getvalue(), (
        "the stock handler no longer dumps on an RST, so the tests below prove nothing: %r"
        % err.getvalue()[:400])


def t_a_client_that_hangs_up_prints_no_traceback() -> None:
    """THE BUG'S SIGNATURE. Connect, reset, repeat — the server must stay quiet on stderr."""
    srv, port, stop = _serve()
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            for _ in range(3):
                _abort(port)
            time.sleep(0.7)                            # let the handler threads finish
    finally:
        stop()
    out = err.getvalue()
    assert "Traceback" not in out, (
        "a dropped client still dumps a traceback on the operator's only view of C4:\n%s" % out)
    assert "ConnectionResetError" not in out, out


def t_the_server_still_answers_after_a_client_drops() -> None:
    """Swallowing the reset must not wedge the handler thread or the listener. A real /health after
    three hang-ups — this is the property that actually matters, since the whole complaint was a
    solver believed dead."""
    srv, port, stop = _serve()
    try:
        for _ in range(3):
            _abort(port)
        body = urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=10).read()
        d = json.loads(body)
        assert d.get("ok") is True, d
        # IDENTITY, not just 200: /health returning these keys is what tells the crawler it reached
        # the solver and not some other service that happens to hold the port.
        assert "browser_open" in d and "solved_hosts" in d, d
    finally:
        stop()


def t_a_second_instance_cannot_co_bind_the_port() -> None:
    """On Windows SO_REUSEADDR lets a second process bind a port that is already being LISTENED on,
    so 'run exactly one instance' was unenforceable. main() turns allow_reuse_address off there."""
    src = _SRC.read_text(encoding="utf-8")
    # Comments stripped: this file and the source both explain the bug at length and must not match
    # their own prose.
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
    assert "allow_reuse_address" in code, (
        "main() never touches allow_reuse_address — HTTPServer defaults it to 1, and on Windows "
        "that lets a second solver co-bind the port and answer solves it cannot perform")
    assert 'os.name != "nt"' in code or "os.name != 'nt'" in code, (
        "the reuse rule must be Windows-specific: on POSIX allow_reuse_address=1 is what lets the "
        "solver restart without waiting out TIME_WAIT")

    if os.name == "nt":
        srv, port, stop = _serve()
        try:
            hs.ThreadingHTTPServer.allow_reuse_address = False
            try:
                second = ThreadingHTTPServer(("127.0.0.1", port), hs.Handler)
                second.server_close()
                raise AssertionError("a second server bound a port already in LISTEN — the "
                                     "two-solver failure is still possible")
            except OSError:
                pass                                   # refused, which is the point
        finally:
            hs.ThreadingHTTPServer.allow_reuse_address = True
            stop()


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  %s" % fn.__name__)
    print("ALL %d HOST-SOLVER TESTS PASSED" % len(fns))


def test_host_solver_suite() -> None:
    main()


if __name__ == "__main__":
    main()
