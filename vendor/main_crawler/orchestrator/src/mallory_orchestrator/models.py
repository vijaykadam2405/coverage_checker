"""Control-plane tables: the Source Catalog, the Coverage ledger, and the Job run log."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Source(Base):
    """One row per registrable domain (eTLD+1). Human adds {url, frequency, category};
    everything else (source_id, tier, source_known, accept_rate) is automatic."""

    __tablename__ = "sources"
    domain: Mapped[str] = mapped_column(String, primary_key=True)  # eTLD+1
    source_id: Mapped[str] = mapped_column(String, index=True)
    category: Mapped[str] = mapped_column(String)
    tier: Mapped[int] = mapped_column(Integer)  # 1..3 (confidence weight only, NOT relevance)
    frequency: Mapped[str] = mapped_column(String, default="daily")  # 6h|daily|weekly — crawl cadence
    region: Mapped[str | None] = mapped_column(String, nullable=True)
    source_known: Mapped[bool] = mapped_column(Boolean, default=True)
    tier_origin: Mapped[str] = mapped_column(String, default="human")  # human|heuristic|learned
    added_by: Mapped[str] = mapped_column(String, default="human")  # human|auto
    # Optional explicit crawl targets (used for the offline end-to-end test); production builds
    # search URLs from the seed instead.
    seed_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    search_template: Mapped[str | None] = mapped_column(String, nullable=True)  # {q} placeholder
    accept_rate: Mapped[float | None] = mapped_column(nullable=True)
    last_crawled: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class SourceRow(Base):
    """The operator's ORIGINAL spreadsheet row, kept verbatim alongside the resolved Source.

    Two reasons this is a side-car table rather than columns on `sources`:
      * the Sources tab shows the operator their OWN column headings — we cannot know them ahead of
        time, so the raw row has to survive import as free-form JSON;
      * this repo has no Alembic and create_all() only ever adds new TABLES, never columns.

    `params` holds the normalised crawl budget (max_pages / max_depth / frequency / region) that the
    job generator reads, so a spreadsheet's "Estimated Max Pages" column actually reaches the crawler
    instead of being silently replaced by a default.
    """

    __tablename__ = "source_rows"
    domain: Mapped[str] = mapped_column(String, primary_key=True)   # == Source.domain
    raw: Mapped[dict] = mapped_column(JSON)                          # the spreadsheet row, verbatim
    params: Mapped[dict] = mapped_column(JSON)                       # {max_pages, max_depth, frequency, region, notes}
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime)
    source_file: Mapped[str | None] = mapped_column(String, nullable=True)


class CoverageCell(Base):
    """One (source × entity) cell of the required coverage matrix — the no-miss ledger."""

    __tablename__ = "coverage_cells"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # f"{domain}|{entity_id}"
    domain: Mapped[str] = mapped_column(String, index=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cadence: Mapped[str] = mapped_column(String, default="daily")
    last_fetched: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_emitted: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="never")  # never|fresh|stale


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String, index=True)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dispatched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    kept: Mapped[int] = mapped_column(Integer, default=0)
    records_emitted: Mapped[int] = mapped_column(Integer, default=0)
    records_forwarded: Mapped[int] = mapped_column(Integer, default=0)  # dead since L2 removed; kept for UI compat
    l2_accepted: Mapped[int] = mapped_column(Integer, default=0)        # dead since L2 removed; kept for UI compat
    status: Mapped[str] = mapped_column(String, default="ok")  # ok|error|skipped
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── Control-panel tables (scheduler, keyword bank, app settings) ──────────────
# Datetimes are stored as NAIVE UTC on purpose: the SQLite dialect hands back naive
# datetimes even for timezone=True columns, so a scheduler that compares next_run to
# now() must keep both naive to avoid aware/naive TypeErrors. scheduler.utcnow() mints them.

class ScheduledJob(Base):
    """A crawl job registered to fire on a cadence. `job` is the full crawler Job dict,
    dispatched verbatim to the crawler API. next_run NULL = will never fire."""

    __tablename__ = "scheduled_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    job: Mapped[dict] = mapped_column(JSON)                       # full crawler Job payload
    job_type: Mapped[str] = mapped_column(String, index=True)     # denormalized from job["job_type"]
    frequency: Mapped[str] = mapped_column(String, default="once")  # once|6h|daily|weekly
    retry_count: Mapped[int] = mapped_column(Integer, default=0)   # configured 0..3
    retries_left: Mapped[int] = mapped_column(Integer, default=0)  # working counter
    start_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    next_run: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String, default="pending")  # pending|running|ok|retrying|error|done
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ScheduleLog(Base):
    """Audit trail of scheduler + schedule-CRUD events (shown on the Logs page)."""

    __tablename__ = "schedule_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # no FK: logs outlive deletes
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # scheduled|enabled|paused|removed|dispatched|completed|retry|failed|skipped_busy
    #   |master_on|master_off|category_on|category_off|skipped_category
    # Full-cycle outcome badges (one per completed run, alongside `completed`):
    #   new_data    — re-crawl found new/changed pages (kept > 0)
    #   no_new_data — re-crawl found nothing changed
    #   baseline    — first-ever crawl of the site (everything is new by definition)
    event: Mapped[str] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class Keyword(Base):
    """Keyword bank: terms that augment generated jobs by group (does NOT touch the crawler's gate corpus)."""

    __tablename__ = "keywords"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String, unique=True, index=True)
    group_label: Mapped[str] = mapped_column(String, default="general")  # serialized as "group" in the DTO
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)


class Setting(Base):
    """Tiny key/value store for app-level flags (e.g. pipeline_enabled = 'true'/'false')."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


# ── Access control ────────────────────────────────────────────────────────────
# This dashboard can stop a crawl, delete the corpus and read every stored page, and it is reachable
# by anyone who can open the tunnel. Two factors, because a password alone on an operator console is
# one leaked credential away from someone else pressing Flush.
#
# NOTHING REVERSIBLE IS STORED. Passwords are scrypt hashes with a per-user salt; the OTP is stored
# as a SHA-256 hash and compared in constant time; session tokens are stored hashed too, so a copy
# of this database does not hand over a single live session.

class User(Base):
    """One operator. `email` is where that person's OTP is sent — it is the second factor, so it is
    a property of the ACCOUNT and never accepted from the login form."""

    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String)
    # scrypt, per-user random salt. Parameters are stored per row so they can be raised later
    # without invalidating existing passwords.
    password_hash: Mapped[str] = mapped_column(String)
    password_salt: Mapped[str] = mapped_column(String)
    kdf: Mapped[str] = mapped_column(String, default="scrypt")
    kdf_params: Mapped[dict] = mapped_column(JSON, default=dict)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Consecutive failed password attempts. Cleared on success; a lockout is what stops an
    # unthrottled console from being brute-forced at HTTP speed.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class LoginChallenge(Base):
    """A password that checked out, waiting on its emailed code.

    Deliberately a separate row rather than a flag on the user: the password step must not leave the
    account in a half-authenticated state, and an abandoned challenge has to expire on its own.
    """

    __tablename__ = "login_challenges"
    id: Mapped[str] = mapped_column(String, primary_key=True)      # random, opaque to the client
    username: Mapped[str] = mapped_column(String, index=True)
    otp_hash: Mapped[str] = mapped_column(String)                  # sha256 of the 6 digits
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    sent_to: Mapped[str] = mapped_column(String, default="")       # masked, for the UI to echo
    # If the mail could not be sent, the challenge is useless and the UI must say so rather than
    # asking for a code that was never delivered.
    delivery_error: Mapped[str | None] = mapped_column(String, nullable=True)


class Session(Base):
    """A logged-in browser. The cookie carries the token; only its hash is stored here, so the table
    cannot be used to impersonate anyone. Rows are deleted on logout and swept once expired."""

    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)


class DomainShard(Base):
    """Which crawler instance owns a domain. Written once, on first sight, and then never changed.

    The fleet is N crawler containers and each keeps its own `crawl_history.sqlite` — so a domain's
    durable frontier physically lives inside one container. A pure `hash(domain) % N` would be
    stable only while N is: adding an instance moves most domains, and their frontiers do not move
    with them, so every moved site would restart from its seed while its real progress sat
    unreachable next door. Recording the assignment makes growth free.

    Keyed on the same normalised host everything else uses: lowercase, `www.` stripped.
    """

    __tablename__ = "domain_shards"
    domain: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[int] = mapped_column(Integer, index=True)


# ── Adaptive crawl cadence (baseline → probe → steady) ────────────────────────
# Deliberately a SIDE-CAR table keyed 1:1 to Source.domain instead of new columns on `sources`:
# this repo has no Alembic, and Base.metadata.create_all only creates NEW TABLES — it never adds
# columns to an existing one. A side-car deploys with zero migration.

class SiteSchedule(Base):
    """Per-site crawl policy: lifecycle phase, cadence tier, measured change rate, cost, and the
    day bucket it was levelled into. One row per registrable domain."""

    __tablename__ = "site_schedules"
    domain: Mapped[str] = mapped_column(String, primary_key=True)       # == Source.domain (eTLD+1)
    source_id: Mapped[str] = mapped_column(String, index=True)          # joins to job["source_id"]

    # baseline = never crawled (census); probe = crawled once, gathering change signal;
    # steady = tiering from measured data; paused = operator/no-signal halt.
    phase: Mapped[str] = mapped_column(String, default="baseline")
    tier: Mapped[str] = mapped_column(String, default="weekly")         # daily|weekly|monthly
    # why the tier is what it is — bootstrap|measured|floor|cost_cap|no_signal|manual
    tier_reason: Mapped[str] = mapped_column(String, default="bootstrap")
    tier_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    defence_score: Mapped[float] = mapped_column(Float, default=0.0)    # 0..1, set by the jobgen probe
    defence_score_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    # λ = CHANGES PER DAY, not per run: a per-run rate is self-reinforcing (a monthly site always
    # looks "very changed" per crawl and would ratchet itself back to daily). Only a time-normalised
    # rate is cadence-invariant.
    lambda_ewma: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_rate_ewma: Mapped[float | None] = mapped_column(Float, nullable=True)  # kept/checked (diagnostic)
    measurements: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_zero_checked: Mapped[int] = mapped_column(Integer, default=0)
    pending_tier: Mapped[str | None] = mapped_column(String, nullable=True)  # hysteresis vote
    pending_votes: Mapped[int] = mapped_column(Integer, default=0)

    pages_ewma: Mapped[float | None] = mapped_column(Float, nullable=True)
    seconds_ewma: Mapped[float | None] = mapped_column(Float, nullable=True)
    seconds_m2: Mapped[float | None] = mapped_column(Float, nullable=True)   # Welford → p90 forecast band
    careful: Mapped[bool] = mapped_column(Boolean, default=False)            # .gov/.mil → C3, 20-50x cost
    sentinel: Mapped[bool] = mapped_column(Boolean, default=False)           # cheap depth-0 change probe

    assigned_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0..6 (weekly tier)
    assigned_dom: Mapped[int | None] = mapped_column(Integer, nullable=True)      # 1..28 (monthly tier)
    slot_minute: Mapped[int] = mapped_column(Integer, default=0)                  # 0..1439, spread in-day

    baseline_done_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_measured_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class SiteMeasurement(Base):
    """Append-only observation, one row per successful non-baseline run. Keeps tiering auditable and
    lets λ be recomputed from scratch if the smoothing constants ever change."""

    __tablename__ = "site_measurements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String, index=True)
    job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    interval_days: Mapped[float] = mapped_column(Float, default=0.0)
    tier_at_run: Mapped[str | None] = mapped_column(String, nullable=True)
    is_sentinel: Mapped[bool] = mapped_column(Boolean, default=False)
    # raw crawler counters, verbatim
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    kept: Mapped[int] = mapped_column(Integer, default=0)
    skipped_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    skipped_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    not_modified_304: Mapped[int] = mapped_column(Integer, default=0)
    dropped_by_gate: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    recovered: Mapped[int] = mapped_column(Integer, default=0)
    # derived
    checked: Mapped[int] = mapped_column(Integer, default=0)
    change_rate: Mapped[float] = mapped_column(Float, default=0.0)
    changes_per_day: Mapped[float] = mapped_column(Float, default=0.0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)


# ── the DAILY lane ────────────────────────────────────────────────────────────
# Its own table, not a `frequency="daily"` ScheduledJob, because the two lanes must never see each
# other's rows: they run as two independent loops, and a row visible to both would be dispatched
# twice — once to the reserved instance and once to whichever instance owns its domain, each with
# its own frontier. One table per lane is what makes double dispatch structurally impossible
# rather than a thing a busy-flag has to prevent.

class Campaign(Base):
    """A saved, re-runnable ad-hoc crawl plan (campaigns.py): the operator's list and options, the
    plan built from them (tier per site, phase jobs, estimates), and what happened when it ran.

    `plan` is the artefact the operator approved — the jobs in it are dispatched VERBATIM on Go, so
    the preview and the run cannot differ. `progress` is the last snapshot: pages held per site at
    launch (so "captured" is a real delta), which instance took which jobs, the phase reached.
    """

    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)  # draft|running|finished|stopped
    options: Mapped[dict] = mapped_column(JSON)                  # window, scope, pdfs
    sites: Mapped[list] = mapped_column(JSON)                    # the seeds as entered, normalised
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    last_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class DailyUrl(Base):
    """One site crawled every day on the reserved crawler instance.

    Keyed on the URL because that is what the operator edits (seed_data/daily_urls.txt); `domain` is
    the normalised host, carried so the API can show which of these still collide with a catalogue
    shard assignment made before the instance was reserved.
    """

    __tablename__ = "daily_urls"
    url: Mapped[str] = mapped_column(String, primary_key=True)
    domain: Mapped[str] = mapped_column(String, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)   # the '#' comment from the file
    next_run: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String, default="pending")  # pending|running|ok|error
    last_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cumulative, so a site that has never yielded anything is visible without joining the log.
    runs: Mapped[int] = mapped_column(Integer, default=0)
    last_kept: Mapped[int] = mapped_column(Integer, default=0)
    last_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
