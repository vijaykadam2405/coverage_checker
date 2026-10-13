"""Orchestrator configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # Control-plane store (SQLite by default; swap to Postgres via DATABASE_URL for scale).
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./orchestrator.db")
    # Downstream worker. `crawler_api` stays the single canonical base URL — it is instance 0, the
    # one that has always existed and still holds the bulk of the corpus's frontier.
    crawler_api: str = os.environ.get("CRAWLER_API", "http://localhost:8099")
    # The FLEET. Comma-separated, in instance order; `crawler_api` is used when it is unset, so a
    # one-instance deployment behaves exactly as it did before this existed. Order is load-bearing:
    # the index into this list IS the instance number recorded in domain_shards, so reordering it
    # sends sites to a container that does not hold their frontier.
    crawler_api_pool: str = os.environ.get("CRAWLER_API_POOL", "")
    # The ingest landing owns the REAL document store (Postgres `documents` + the MinIO bucket).
    # The orchestrator's own DB is a control plane only — catalog, schedule, run history — so every
    # "what did we actually capture" number on the dashboard is read from here, not invented locally.
    ingest_api: str = os.environ.get("INGEST_API_URL", "http://localhost:9090")
    # A crawl runs until the site is exhausted, so the dispatch read-timeout must be generous.
    dispatch_timeout: float = float(os.environ.get("DISPATCH_TIMEOUT", "3600"))
    # Scheduler tick interval (seconds).
    scheduler_tick: int = int(os.environ.get("SCHEDULER_TICK", "20"))
    # ── the DAILY lane ────────────────────────────────────────────────────────
    # Indices into crawler_api_pool of the instances that run the daily URL list. Comma-separated,
    # e.g. "0,1,2". Empty (default) means no lane, and every line of daily-lane code then no-ops:
    # an existing deployment behaves exactly as it did before this existed.
    #
    # CRAWLER_DAILY_INSTANCE (singular, an int, -1 for none) is the older single-instance form and
    # is still honoured when the plural is unset, so nothing that already sets it changes meaning.
    daily_instances_raw: str = os.environ.get("CRAWLER_DAILY_INSTANCES", "")
    daily_instance: int = int(os.environ.get("CRAWLER_DAILY_INSTANCE", "-1"))
    # What "reserved" means, and it is a real trade-off with no free answer.
    #
    #   exclusive=1  the lane's instances NEVER take catalogue work. shard.py refuses to assign a
    #                domain to them, so a 40-minute daily re-crawl can never queue behind a six-hour
    #                catalogue crawl. Punctuality is guaranteed; the instances idle once the daily
    #                list is clear.
    #   exclusive=0  (default) the lane has PRIORITY on those instances rather than sole use: while
    #                any daily site is due, catalogue work bound for them is held; once the list is
    #                clear for the day they rejoin the general fleet and work the backlog.
    #
    # The cost of exclusive=0 is bounded and worth stating plainly: a catalogue crawl already
    # running on one of those instances is not interrupted, so the day's first daily sweep can be
    # delayed by up to the length of that crawl. Rows keep their next_run and fire on the first free
    # tick, so the delay is never a lost day — but "daily" does become "daily, once the instance
    # frees up". daily.overdue() surfaces that rather than letting it pass silently.
    daily_exclusive: bool = os.environ.get("CRAWLER_DAILY_EXCLUSIVE", "0").lower() in (
        "1", "true", "yes", "on")
    # Daily-lane tick. Slower than the main scheduler because its queue is 37 sites/day, not 500.
    daily_tick: int = int(os.environ.get("DAILY_TICK", "60"))
    # Bundled static seed (watchlist + source registry).
    seed_dir: str = os.environ.get("SEED_DIR", "./seed_data")
    cors_origins: str = os.environ.get("CORS_ORIGINS", "*")


def get_settings() -> Settings:
    return Settings()


def daily_instance_list(s: Settings | None = None) -> list[int]:
    """The lane's instance indices, de-duplicated and in ascending order.

    Reads os.environ directly rather than the Settings snapshot. `Settings` is a dataclass whose
    field defaults are evaluated when the CLASS is created — i.e. once, at import — so
    `Settings()` returns the environment as it was when the module first loaded, not as it is now.
    That is invisible in production (env is set before import) and wrong everywhere else: it makes
    the lane untestable and makes a mid-life env change silently ineffective.

    Plural wins; singular is the fallback so an existing CRAWLER_DAILY_INSTANCE keeps working.
    Junk entries are dropped rather than raising: a typo in one index must not stop the
    orchestrator from booting, and daily.reservation() reports what was actually resolved.
    """
    raw = os.environ.get("CRAWLER_DAILY_INSTANCES")
    if raw is None:
        raw = (s or get_settings()).daily_instances_raw
    raw = (raw or "").strip()
    if raw:
        out: set[int] = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                v = int(part)
            except ValueError:
                continue
            if v >= 0:
                out.add(v)
        return sorted(out)
    try:
        one = int(os.environ.get("CRAWLER_DAILY_INSTANCE",
                                 str((s or get_settings()).daily_instance)))
    except ValueError:
        one = -1
    return [one] if one >= 0 else []


def daily_exclusive_enabled(s: Settings | None = None) -> bool:
    """Whether the lane owns its instances outright. Read live, for the same reason
    daily_instance_list is — see its docstring."""
    raw = os.environ.get("CRAWLER_DAILY_EXCLUSIVE")
    if raw is None:
        return bool((s or get_settings()).daily_exclusive)
    return raw.lower() in ("1", "true", "yes", "on")
