"""A memory limit the engine cannot see is a limit that OOM-kills it.

`_ram_percent` feeds the AIMD render limiter: CRAWLER_RAM_TARGET (85) gates growth and
CRAWLER_RAM_HARD (92) sheds load outright. It read /proc/meminfo — which inside a container is the
HOST. So an instance capped at 28g on a 1007 GiB box reads ~10% and never brakes, no matter how
close to its cgroup ceiling it gets.

Measured consequence: three instances nearly OOMed at a 20g cap while the host had 845 GB free. The
response then was to raise the cap to 40g, which treats the symptom — the governor was never going
to fire at ANY cap. This is what makes a RAM budget enforceable at all: below the cap the engine
throttles itself instead of being killed.

Runnable directly:  python tests/test_ram_brake_sees_its_own_cap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402

GIB = 1024 ** 3


@pytest.fixture()
def cg(tmp_path, monkeypatch):
    """A fake cgroup v2 directory, swapped in by patching open() targets via chdir-free paths."""
    real_open = open

    files: dict[str, str] = {}

    def fake_open(path, *a, **k):
        p = str(path)
        if p in files:
            import io
            return io.StringIO(files[p])
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    return files


def _v2(files, limit, current, inactive_file=0):
    files["/sys/fs/cgroup/memory.max"] = str(limit)
    files["/sys/fs/cgroup/memory.current"] = str(current)
    files["/sys/fs/cgroup/memory.stat"] = "anon 1\ninactive_file %d\nslab 2\n" % inactive_file


def test_pressure_is_measured_against_the_cap_not_the_host(cg):
    """28 GiB cap, 26 GiB used = 93% — over CRAWLER_RAM_HARD. The host reading would say ~10%."""
    _v2(cg, 28 * GIB, 26 * GIB)
    assert ae._cgroup_ram_percent() == pytest.approx(92.9, abs=0.2)
    assert ae._ram_percent() >= 92, "the hard brake must be able to fire under a container cap"


def test_a_healthy_container_reads_low(cg):
    """Negative control. Without it, a reader stuck at 100 would brake permanently and the crawl
    would look like a throughput bug rather than a governor bug."""
    _v2(cg, 28 * GIB, 4 * GIB)
    assert ae._cgroup_ram_percent() == pytest.approx(14.3, abs=0.2)


def test_page_cache_is_not_pressure(cg):
    """memory.current includes reclaimable page cache. Charging it would have the crawler shedding
    renders because it read some files — 20 GiB of cache on a 28 GiB cap is 71% of nothing."""
    _v2(cg, 28 * GIB, 24 * GIB, inactive_file=20 * GIB)
    assert ae._cgroup_ram_percent() == pytest.approx(14.3, abs=0.2)


def test_an_uncapped_cgroup_falls_back_to_the_host_reading(cg):
    """`max` means no limit — every uncapped deployment must behave exactly as before."""
    cg["/sys/fs/cgroup/memory.max"] = "max"
    assert ae._cgroup_ram_percent() is None
    v = ae._ram_percent()
    assert 0.0 <= v <= 100.0


def test_a_host_scale_v1_limit_is_not_a_cap(cg):
    """cgroup v1 reports "unlimited" as a number near 2^63. Treating that as a denominator gives
    0.0% forever, which silently disables the brake — the same failure in a new disguise."""
    cg["/sys/fs/cgroup/memory/memory.limit_in_bytes"] = str((1 << 63) - 4096)
    cg["/sys/fs/cgroup/memory/memory.usage_in_bytes"] = str(4 * GIB)
    assert ae._cgroup_ram_percent() is None


def test_v1_is_read_when_v2_is_absent(cg):
    cg["/sys/fs/cgroup/memory/memory.limit_in_bytes"] = str(20 * GIB)
    cg["/sys/fs/cgroup/memory/memory.usage_in_bytes"] = str(19 * GIB)
    assert ae._cgroup_ram_percent() == pytest.approx(95.0, abs=0.2)


def test_no_cgroup_at_all_is_not_an_error(cg):
    """Windows, macOS, a bare process. Must return None, never raise — this runs in the hot loop."""
    assert ae._cgroup_ram_percent() is None


# ── the same blindness, in CPU ───────────────────────────────────────────────
#
# `psutil.cpu_percent()` is host-wide. Pinned to 20 of 40 cores, a crawler whose own allocation is
# completely saturated reads 50% — under CRAWLER_CPU_TARGET (85) — so the governor keeps admitting
# renders into cores with nothing left. Over-admission on a starved lane has cost this fleet
# throughput twice already.


def _proc_stat(busy_by_cpu: dict[int, tuple[int, int]]) -> str:
    """busy_by_cpu: {cpu_index: (busy_jiffies, idle_jiffies)} -> a /proc/stat body."""
    out = ["cpu  0 0 0 0 0 0 0 0 0 0"]
    for n, (b, i) in sorted(busy_by_cpu.items()):
        out.append("cpu%d %d 0 0 %d 0 0 0 0 0 0" % (n, b, i))
    out.append("intr 0\nctxt 0\n")
    return "\n".join(out)


@pytest.fixture()
def pinned(cg, monkeypatch):
    """Pin to cores 0-19 of a 40-core box and drive /proc/stat by hand."""
    monkeypatch.setattr(ae.os, "sched_getaffinity", lambda _p: set(range(20)), raising=False)
    monkeypatch.setattr(ae.os, "cpu_count", lambda: 40)
    ae._CPUSET_PREV.clear()
    yield cg
    ae._CPUSET_PREV.clear()


def test_our_own_cores_saturated_reads_saturated_not_half(pinned):
    """Cores 0-19 pegged, 20-39 idle. Host-wide that is 50%; the honest answer is 100%."""
    hot = {n: (0, 0) for n in range(20)}
    cold = {n: (0, 0) for n in range(20, 40)}
    pinned["/proc/stat"] = _proc_stat({**hot, **cold})
    assert ae._cpuset_cpu_percent() is None, "the first call primes the delta"
    # our 20 cores each gain 100 busy jiffies; the other 20 gain 100 idle
    hot = {n: (100, 0) for n in range(20)}
    cold = {n: (0, 100) for n in range(20, 40)}
    pinned["/proc/stat"] = _proc_stat({**hot, **cold})
    assert ae._cpuset_cpu_percent() == pytest.approx(100.0, abs=0.2)


def test_idle_allocation_reads_idle(pinned):
    """Negative control: a reader stuck at 100 would permanently pin the render limit at its floor
    and look exactly like a slow-sites problem."""
    pinned["/proc/stat"] = _proc_stat({n: (0, 0) for n in range(40)})
    ae._cpuset_cpu_percent()
    pinned["/proc/stat"] = _proc_stat({**{n: (10, 90) for n in range(20)},
                                       **{n: (99, 1) for n in range(20, 40)}})
    assert ae._cpuset_cpu_percent() == pytest.approx(10.0, abs=0.5)


def test_an_unpinned_container_keeps_the_host_reading(cg, monkeypatch):
    """No cpuset means the affinity mask is every CPU. Returning a number there would silently
    change the tuning of every deployment that is not pinned."""
    monkeypatch.setattr(ae.os, "sched_getaffinity", lambda _p: set(range(40)), raising=False)
    monkeypatch.setattr(ae.os, "cpu_count", lambda: 40)
    cg["/proc/stat"] = _proc_stat({n: (1, 1) for n in range(40)})
    assert ae._cpuset_cpu_percent() is None


def test_the_governor_prefers_the_cpuset_reading():
    """Pins the wiring, not just the helper — the helper returning the right number is worthless if
    _drain still feeds psutil's host-wide value to the limiter. Comments stripped: this suite has
    three times reported a green result that only matched its own prose."""
    import inspect
    import re

    src = re.sub(r"#.*", "", inspect.getsource(ae.AsyncEngine._drain))
    assert "_cpuset_cpu_percent()" in src, "_drain never consults the cpuset reading"
    assert re.search(r"cpu\s*=\s*host_cpu if pinned is None else pinned", src), (
        "the limiter must use the cpuset reading when pinned and the host reading otherwise")
    assert "psutil.cpu_percent(interval=None)" in src, (
        "psutil is a stateful delta — skipping the call corrupts the next host-wide reading")


def test_the_brake_actually_sheds_load_at_that_reading():
    """Ties the reading to the behaviour it exists for, so a future refactor of _aimd_limit cannot
    leave this file passing while the render pool keeps growing into an OOM."""
    shed = ae._aimd_limit(cpu=10.0, limit=100, target=90, step_up=4, lo=1, hi=200,
                          ram=93.0, ram_target=85.0, ram_hard=92.0)
    assert shed < 100, "ram >= ram_hard must shed load even with CPU idle"
    grow = ae._aimd_limit(cpu=10.0, limit=100, target=90, step_up=4, lo=1, hi=200,
                          ram=14.0, ram_target=85.0, ram_hard=92.0)
    assert grow > 100, "a container with RAM headroom must still be allowed to grow"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
