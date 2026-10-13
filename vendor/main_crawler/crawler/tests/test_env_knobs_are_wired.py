"""A knob you can SET must be a knob that arrives. Anything in .env must be named in compose.

THIS TRAP HAS BEEN HIT FOUR TIMES. Compose only passes through the environment variables it names
explicitly, so a variable set in .env that compose does not mention never reaches the container:
the code reads its default, Docker reports nothing, and the setting appears to be ignored for no
reason. Measured instances: CRAWLER_JOBS_PER_INSTANCE, CRAWLER_AUTO_REFILL, and most recently
CRAWLER_CAREFUL_TTL_H — a mark-expiry knob shipped the night before, unreachable from .env, while
the very problem it was meant to tune (hosts re-throttling to concurrency 1 within hours of being
cleared) was live on the fleet.

Scope is deliberate. An earlier version of this file asserted that EVERY CRAWLER_* the code reads
must be in compose, and it reported ~50 — because most are code-default tuning constants nobody
ever sets, and demanding they all be plumbed is noise rather than signal. The bug is not "a knob
has a default". The bug is "someone set it and it did nothing", so the subject is the settable
surface: .env.example, which is the documented contract for what an operator may configure.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.datacentre.yml",
                 "docker-compose.scale.yml", "docker-compose.solver.yml",
                 "docker-compose.tunnel.yml"]
ENV_SAMPLE = REPO / ".env.example"

# Consumed by the shell/scripts around compose rather than by a service it starts.
_EXEMPT = {"CRAWLER_DATA_ROOT"}


def _compose_names() -> set[str]:
    names: set[str] = set()
    seen = False
    for f in COMPOSE_FILES:
        p = REPO / f
        if p.exists():
            seen = True
            names |= set(re.findall(r"([A-Z][A-Z0-9_]{2,})", p.read_text(encoding="utf-8")))
    if not seen:
        pytest.skip("no docker-compose files found next to the crawler package")
    return names


def _env_sample_names() -> dict[str, int]:
    if not ENV_SAMPLE.exists():
        pytest.skip(".env.example not found")
    out: dict[str, int] = {}
    for n, line in enumerate(ENV_SAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        out.setdefault(s.split("=", 1)[0].strip(), n)
    return out


def test_every_documented_setting_is_passed_through_by_compose():
    declared = _env_sample_names()
    assert declared, ".env.example declares nothing — the scanner is broken, not the config"
    wired = _compose_names()
    missing = {k: v for k, v in declared.items() if k not in wired and k not in _EXEMPT}
    assert not missing, (
        "these are documented as settable in .env.example but no compose file names them, so "
        "setting them does NOTHING and the code silently uses its default:\n" +
        "\n".join("  %-38s .env.example:%d" % (k, v) for k, v in sorted(missing.items())))


def test_the_knobs_this_test_was_written_for():
    """Named explicitly so a refactor cannot quietly drop them and stay green.

    INGEST_ROLLUP_TTL_S is in this list because the FIRST version of this test missed it: the scan
    was written against .env.example, and that knob was neither documented there nor wired into
    compose. So the trap recurred a fifth time — a 30s cache TTL in front of a 33s query, which
    meant the query ran continuously and the dashboard reported "corpus unreachable" over a
    healthy database. The scan itself is prefix-agnostic; what failed was that the knob was
    undocumented, which is now also fixed."""
    wired = _compose_names()
    for k in ("CRAWLER_CAREFUL_TTL_H", "CRAWLER_HOST_WEAK_FAILS",
              "CRAWLER_JOBS_PER_INSTANCE", "CRAWLER_AUTO_REFILL",
              "INGEST_ROLLUP_TTL_S", "INGEST_QUALITY_TTL_S",
              "INGEST_MINIO_TTL_S"):
        assert k in wired, "%s is not named in any compose file" % k


def test_the_scanner_would_catch_an_unwired_setting():
    """Guard against a green result that only means the scan found nothing. Runs the same set
    difference the real assertion runs, against a name compose provably does not contain."""
    wired = _compose_names()
    declared = dict(_env_sample_names())
    declared["CRAWLER_DEFINITELY_NOT_WIRED"] = 0
    missing = {k for k in declared if k not in wired and k not in _EXEMPT}
    assert missing == {"CRAWLER_DEFINITELY_NOT_WIRED"}, (
        "the scan should flag exactly the planted name and nothing else; got %r" % sorted(missing))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
