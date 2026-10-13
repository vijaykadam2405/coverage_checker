"""What has to hold before a second crawler instance can exist beside the first.

Two instances are isolated by pointing `CRAWLER_DATA_DIR` at different directories. That only works
if EVERY piece of per-instance state actually follows it — and two did not:

  issues.jsonl          defaulted to a bare relative "data/issues.jsonl"
  paid_proxy_usage.json defaulted to the absolute "/app/data/paid_proxy_usage.json"

Sharing either is worse than it sounds. `IssueLog.clear_all()` truncates the whole file, and the
paid-proxy counter is a read-modify-write full overwrite — two writers both read N and both write
N+1, so the daily spend cap is silently exceeded rather than merely mis-reported.

The third test covers the single-flight guard, which was gated on `background=True` and so was
bypassed by the synchronous path the orchestrator's manual batch actually uses.

Runnable directly:  python tests/test_instance_isolation.py
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _reload_with_data_dir(mod_name: str, data_dir: str):
    """Re-import a module with CRAWLER_DATA_DIR set — both defaults are bound at import time."""
    os.environ["CRAWLER_DATA_DIR"] = data_dir
    os.environ.pop("CRAWLER_ISSUES_PATH", None)
    os.environ.pop("PAID_PROXY_USAGE_FILE", None)
    from crawler import config
    importlib.reload(config)
    mod = importlib.import_module(mod_name)
    return importlib.reload(mod)


def test_two_instances_get_two_issue_logs():
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    issues = _reload_with_data_dir("crawler.issues", a)
    pa = issues.IssueLog().path
    issues = _reload_with_data_dir("crawler.issues", b)
    pb = issues.IssueLog().path
    assert pa != pb, f"both instances write the same issue log: {pa}"
    assert str(pa).startswith(a) and str(pb).startswith(b), (pa, pb)


def test_two_instances_get_two_paid_proxy_counters():
    """The money one. A shared counter is not merged, it is LOST — both read N, both write N+1."""
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    pp = _reload_with_data_dir("crawler.paid_proxy", a)
    ua = pp._USAGE_PATH
    pp = _reload_with_data_dir("crawler.paid_proxy", b)
    ub = pp._USAGE_PATH
    assert ua != ub, f"both instances share one spend counter: {ua}"
    assert ua.startswith(a) and ub.startswith(b), (ua, ub)


def test_an_explicit_path_still_wins():
    """The override has to keep working — compose sets these today."""
    d = tempfile.mkdtemp()
    os.environ["CRAWLER_DATA_DIR"] = d
    os.environ["CRAWLER_ISSUES_PATH"] = str(Path(d) / "custom.jsonl")
    os.environ["PAID_PROXY_USAGE_FILE"] = str(Path(d) / "custom.json")
    from crawler import config
    importlib.reload(config)
    issues = importlib.reload(importlib.import_module("crawler.issues"))
    pp = importlib.reload(importlib.import_module("crawler.paid_proxy"))
    assert issues.IssueLog().path.name == "custom.jsonl"
    assert pp._USAGE_PATH.endswith("custom.json")
    os.environ.pop("CRAWLER_ISSUES_PATH", None)
    os.environ.pop("PAID_PROXY_USAGE_FILE", None)


def test_single_flight_covers_the_synchronous_path():
    """`background=False` is the path `orchestrate.run_manual_batch` uses. Two concurrent sync
    batches share one `_batch_status` AND each calls `_arm()`, which clears the stop file — so stop
    stops nothing. Asserted on the source because exercising it needs a live engine."""
    src = (Path(__file__).resolve().parents[1] / "crawler_api" / "app.py").read_text(encoding="utf-8")
    i = src.index("def crawl_batch(")
    body = src[i:i + 3000]
    assert 'if req.background and _batch_status.get("running")' not in body, (
        "the guard is still gated on background=True — the synchronous path bypasses it")
    assert 'owner.is_alive()' in body, (
        "single-flight no longer checks a LIVE owner; guarding on the flag alone means a thread "
        "that died wedges the API until the container restarts")


def test_the_owner_handle_never_reaches_a_json_response():
    """`owner` is a Thread. Serialising it 500s /v1/batch/status, which takes the dashboard with
    it — the panel that exists to tell you whether a crawl is running."""
    src = (Path(__file__).resolve().parents[1] / "crawler_api" / "app.py").read_text(encoding="utf-8")
    i = src.index("def batch_status(")
    assert 'out.pop("owner", None)' in src[i:i + 900], (
        "/v1/batch/status returns the raw status dict, Thread handle and all")


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} INSTANCE-ISOLATION TESTS PASSED")


if __name__ == "__main__":
    main()
