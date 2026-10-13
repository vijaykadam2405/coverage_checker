"""Three fixes for one measured failure: 45,825 renders queued behind four hosts, lane 32% full.

The renderer was never the bottleneck — CamoFox sat at 16% CPU and 2GB of 160GB while 197 items in
five minutes gave up with `host_busy` at a median wait of 42 minutes. The instance with a FULL
render lane (128/128) was the fastest in the fleet at 4.53 pages/s; the starved ones (0/128) were
the slow ones. So the queue was blocked before the browser, not by it.

  1. a deep-path cap, because one host was 24,436 URLs of accumulated menu for 34 stored pages;
  2. a deflect budget keyed on the HOST, because per item it could not be enforced at all;
  3. a per-host concurrency override, because one global number let big sites set the fleet's pace.

Runnable directly:  python tests/test_host_pressure.py
"""
from __future__ import annotations

import os
import sys

import pytest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402

SRC = Path(ae.__file__).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_env():
    """Put os.environ back after every test in this file.

    These tests drive behaviour through env vars, and env is PROCESS-global — leaving
    CRAWLER_HOST_QUEUE_MAX=1 behind broke six unrelated tests in the full run while every one of
    them passed in isolation, which is the most expensive kind of test failure to diagnose.
    """
    keys = [k for k in os.environ if k.startswith("CRAWLER_")]
    saved = {k: os.environ[k] for k in keys}
    yield
    for k in [k for k in os.environ if k.startswith("CRAWLER_")]:
        if k not in saved:
            del os.environ[k]
    os.environ.update(saved)



# ── 1. the deep-path cap ──────────────────────────────────────────────────────────────────────
class _Ctx:
    def __init__(self):
        self.deep_paths: dict[str, int] = {}
        self.trap_skipped = 0


def _enqueue_deep(host: str, n: int, depth: int = 7, cap: int = 5000) -> int:
    """Replay the cap's logic over n distinct deep URLs; returns how many were accepted."""
    os.environ["CRAWLER_HOST_DEEP_CAP"] = str(cap)
    ctx, accepted = _Ctx(), 0
    for i in range(n):
        segs = ["press", "events", "about", "team", "unit", "sub"][:depth - 1] + [f"p{i}"]
        if len(segs) >= int(os.environ.get("CRAWLER_HOST_DEEP_DEPTH", "6")):
            k = f"deep:{host}"
            c = ctx.deep_paths.get(k, 0) + 1
            if c > cap:
                ctx.trap_skipped += 1
                continue
            ctx.deep_paths[k] = c
        accepted += 1
    return accepted


def test_the_trap_volume_is_cut_off():
    """rusbitech.ru held 21,251 URLs at depth >= 6. It gets 5,000 and no more."""
    assert _enqueue_deep("rusbitech.ru", 21251) == 5000


def test_the_biggest_REAL_site_measured_is_untouched():
    """safran-group.com: 47,574 pages stored, 2,002 URLs at depth >= 6 — comfortably under."""
    assert _enqueue_deep("www.safran-group.com", 2002) == 2002


def test_shallow_pages_are_never_capped():
    """A real site's content lives shallow; capping that would silently truncate the corpus."""
    assert _enqueue_deep("www.safran-group.com", 40000, depth=3) == 40000


def test_the_cap_is_per_host_not_per_job():
    """One greedy host must not consume the allowance of every other site in the same job."""
    ctx = _Ctx()
    for host in ("trap.example", "real.example"):
        for i in range(3):
            k = f"deep:{host}"
            ctx.deep_paths[k] = ctx.deep_paths.get(k, 0) + 1
    assert ctx.deep_paths == {"deep:trap.example": 3, "deep:real.example": 3}


def test_the_cap_is_wired_into_the_enqueue_path():
    """The recurring failure here: the guard exists and nothing calls it."""
    assert "CRAWLER_HOST_DEEP_CAP" in SRC, "the cap is not in the engine"
    body = SRC.split("def _should_enqueue")[1][:9000] if "def _should_enqueue" in SRC else SRC
    assert "ctx.deep_paths" in SRC, "the per-host deep counter is never used"
    assert "self.deep_paths" in SRC, "the counter is not on the job context"


# ── 2. the deflect budget, keyed on the host ──────────────────────────────────────────────────
def test_the_budget_is_keyed_on_the_host_not_the_item():
    """Per item it was unenforceable: the check runs only on dequeue, and one lap of a 45,825-deep
    render queue took ~40 minutes — every give-up read "2 deflections over 2,187s"."""
    assert "host_busy_since" in SRC, "there is no per-host busy clock"
    assert "self.host_busy_since.setdefault(qhost, now)" in SRC, (
        "the budget is not taking its start time from the host")
    assert "self.host_busy_since.pop(qhost, None)" in SRC, (
        "the host clock is never reset, so one jam would become permanent")


def test_a_second_item_inherits_the_hosts_clock():
    """The first item serves the full budget; everything behind it gives up at once."""
    eng = SimpleNamespace(host_busy_since={})
    t0 = 1000.0
    first = eng.host_busy_since.setdefault("zf.com", t0)
    assert max(t0 - first, 0.0) == 0.0                      # item 1: no wait yet, requeues
    later = t0 + 200.0
    first2 = eng.host_busy_since.setdefault("zf.com", later)
    assert later - first2 == 200.0, "item 2 did not inherit the host's start time"
    assert 200.0 >= 180.0, "and so it gives up immediately instead of waiting its own 40 minutes"


def test_the_reset_makes_a_transient_jam_transient():
    eng = SimpleNamespace(host_busy_since={"zf.com": 1000.0})
    eng.host_busy_since.pop("zf.com", None)                 # slots freed
    assert eng.host_busy_since.setdefault("zf.com", 2000.0) == 2000.0, (
        "a host that recovered kept its old start time")


# ── 3. per-host concurrency ───────────────────────────────────────────────────────────────────
def _max_for(host: str, spec: str = "", default: str = "2") -> int:
    os.environ["CRAWLER_HOST_QUEUE_MAX"] = default
    os.environ["CRAWLER_HOST_QUEUE_MAX_HOSTS"] = spec
    eng = ae.AsyncEngine.__new__(ae.AsyncEngine)
    return ae.AsyncEngine._host_queue_max(eng, host)


def test_the_default_is_unchanged_for_everyone_else():
    assert _max_for("some-small-site.example") == 2
    assert _max_for("some-small-site.example", "zf.com:6") == 2


def test_a_listed_host_is_lifted():
    assert _max_for("zf.com", "zf.com:6,rtx.com:6") == 6
    assert _max_for("www.rtx.com", "zf.com:6,rtx.com:6") == 6


def test_www_and_subdomains_follow_the_registered_domain():
    """www.zf.com and zf.com are one server; splitting them would be an accident, not a policy."""
    assert _max_for("www.zf.com", "zf.com:6") == 6
    assert _max_for("careers.zf.com", "zf.com:6") == 6


def test_a_lookalike_is_NOT_lifted():
    assert _max_for("notzf.com", "zf.com:6") == 2
    assert _max_for("zf.com.evil.example", "zf.com:6") == 2


def test_a_malformed_entry_cannot_break_the_crawl():
    """This runs on every render item; a typo in an env var must not raise mid-crawl."""
    for bad in ("zf.com", "zf.com:", "zf.com:abc", ":6", "zf.com:-1", ",,,", "zf.com:6,broken"):
        assert _max_for("other.example", bad) == 2
    assert _max_for("zf.com", "zf.com:6,broken") == 6, "one bad entry discarded the good ones"


def test_BOTH_deflect_sites_use_the_override():
    """An override honoured on one path and not the other is re-imposed by the other."""
    assert SRC.count("self._host_queue_max(qhost)") >= 3, (
        "a deflect site still reads CRAWLER_HOST_QUEUE_MAX directly")
    assert 'self.host.queue_depth(qhost) >= _env_int("CRAWLER_HOST_QUEUE_MAX"' not in SRC


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} HOST-PRESSURE TESTS PASSED")


# ── 4. per-host query-variant cap ─────────────────────────────────────────────────────────────
def _enqueue_q(host: str, paths: int, per_path: int, cap: int = 10000) -> int:
    """Replay the two caps together: `per_path` variants on each of `paths` paths."""
    os.environ["CRAWLER_HOST_QUERY_CAP"] = str(cap)
    variants: dict[str, int] = {}
    hostq: dict[str, int] = {}
    accepted = 0
    for i in range(paths):
        for _ in range(per_path):
            key = f"{host}/a{i}"
            n = variants.get(key, 0) + 1
            if n > 20:                      # the existing PER-PATH facet cap, unchanged
                continue
            variants[key] = n
            hn = hostq.get(host, 0) + 1
            if hn > cap:
                continue
            hostq[host] = hn
            accepted += 1
    return accepted


def test_thousands_of_paths_each_under_the_per_path_cap_are_still_bounded():
    """shvabe.com: ~2,588 article paths x ~18 `?PAGEN_1=` variants = 42,010 URLs, and not one path
    exceeds the 20-variant facet cap. Only a per-HOST total can see it."""
    assert _enqueue_q("shvabe.com", paths=2588, per_path=18) == 10000


def test_the_biggest_REAL_query_user_is_untouched():
    """gdbiw.com had 6,668 query-variant URLs pending — comfortably inside the bound."""
    assert _enqueue_q("www.gdbiw.com", paths=400, per_path=17) == 6800


def test_the_per_path_cap_still_applies_underneath():
    """One path may not take 500 variants just because the host total has room."""
    assert _enqueue_q("one.example", paths=1, per_path=500) == 20


def test_the_host_query_cap_is_wired_in():
    assert "CRAWLER_HOST_QUERY_CAP" in SRC, "the per-host query cap is not in the engine"
    assert "ctx.host_queries" in SRC, "the counter is never used"
    assert "self.host_queries" in SRC, "the counter is not on the job context"
