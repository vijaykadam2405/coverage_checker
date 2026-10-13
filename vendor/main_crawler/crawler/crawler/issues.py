"""Unresolved-page log — the pages the crawler could not get, and exactly why.

A crawl produces two kinds of failure. Most are self-correcting: a timeout gets requeued, a 503
cools down and is retried next run, a JS page escalates to a render. Those belong in the per-reason
counters and nowhere else. The other kind is a page that has been given every attempt the ladder
has and still did not come back — and for those, a counter is useless. You need the URL, the number
of attempts it already burned, and a specific cause, because the fix differs completely between
"Cloudflare interactive, provably unclearable" and "the render never reached networkidle".

That is what this holds. One record per URL, deduplicated, with the attempt count accumulating, so
the log answers "what is still broken and what would fix it" rather than "how many things broke".

Design notes:

* **Append-only JSONL, replayed on load, last write per URL wins.** Survives a container restart
  without needing a schema migration, and a truncated final line (killed mid-write) costs one
  record instead of the file.
* **A later success removes the URL.** Without `resolve()` the log would only ever grow, and a page
  that started working again would sit there forever claiming to be broken — which is how an issues
  list stops being read.
* **Thread-safe.** The engine writes from its batch thread; the API reads from request threads.
* Bounded by CRAWLER_ISSUES_MAX so a pathological crawl cannot exhaust memory; the oldest records
  are dropped first and the drop is counted, because a silently truncated log is worse than a short
  one.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _host_key(host: str) -> str:
    """Compare hosts the way every caller NAMES them: lowercased, `www.` stripped.

    Issues are stored under the hostname the URL actually had — abeking.com redirects to www, so
    every one of its issues is filed as `www.abeking.com`. Callers ask by registered domain, which
    is what the dashboard displays and what its own summary key is normalised to. Matching the
    stored value exactly made those two disagree in the worst possible way: the site row said
    "11 unresolved" in red, and the panel that opens when you ask WHY said "nothing unresolved" —
    the count from the summary (normalised), the rows from this filter (not).

    An operator reading that concludes the numbers are made up, which is the one thing a
    diagnostic panel cannot afford. Normalising here fixes it for every caller at once.
    """
    return (host or "").strip().lower().removeprefix("www.")


@dataclass
class Issue:
    url: str
    cause: str                    # edgecase CASE_* / errors reason — the specific obstacle
    action: str = ""              # what the crawler decided to do about it
    evidence: str = ""            # the marker/measurement that justified the cause
    host: str = ""
    job_id: str = ""
    attempts: int = 1             # how many times we tried before giving up
    status: int | None = None
    first_seen: str = field(default_factory=_now)
    last_seen: str = field(default_factory=_now)
    detail: dict = field(default_factory=dict)


class IssueLog:
    def __init__(self, path: str | Path | None = None, max_items: int | None = None):
        # Default UNDER config.DATA_DIR, not a bare relative "data/issues.jsonl". Every other piece
        # of crawler state already follows CRAWLER_DATA_DIR, and this one silently did not — so
        # pointing two crawler instances at separate data directories would still have left them
        # appending to, and truncating, one shared issue log. One env var has to isolate an
        # instance completely or the isolation is a trap.
        env = os.environ.get("CRAWLER_ISSUES_PATH")
        self.path = Path(path) if path else Path(env) if env else (config.DATA_DIR / "issues.jsonl")
        self.max_items = max_items or int(os.environ.get("CRAWLER_ISSUES_MAX", "5000"))
        self._items: dict[str, Issue] = {}
        self._lock = threading.Lock()
        self._dropped = 0
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue          # a torn final line from a kill costs one record, not the file
                url = d.get("url")
                if not url:
                    continue
                if d.get("_resolved"):
                    self._items.pop(url, None)
                    continue
                d.pop("_resolved", None)
                try:
                    self._items[url] = Issue(**d)
                except TypeError:
                    continue          # record written by an older/newer field set — skip, don't crash
        except Exception:
            pass                      # an unreadable log must never stop a crawl from starting

    def _append(self, payload: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass                      # logging must never be the thing that fails a crawl

    # ── writes ───────────────────────────────────────────────────────────────
    # NOTE: see _host_key at the bottom of this module for why reads normalise `www.`.

    def record(self, url: str, cause: str, *, action: str = "", evidence: str = "",
               job_id: str = "", attempts: int = 1, status: int | None = None,
               detail: dict | None = None) -> Issue:
        """Record (or update) an unresolved page. Re-recording the same URL accumulates the attempt
        count rather than creating a second row — the question is "how much have we already spent on
        this page", which a duplicate row cannot answer."""
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            cur = self._items.get(url)
            if cur:
                cur.attempts = max(cur.attempts, attempts)
                cur.cause, cur.action, cur.evidence = cause, action or cur.action, evidence or cur.evidence
                cur.last_seen = _now()
                cur.status = status if status is not None else cur.status
                if detail:
                    cur.detail.update(detail)
                item = cur
            else:
                item = Issue(url=url, cause=cause, action=action, evidence=evidence, host=host,
                             job_id=job_id, attempts=attempts, status=status,
                             detail=detail or {})
                self._items[url] = item
                if len(self._items) > self.max_items:
                    oldest = min(self._items, key=lambda u: self._items[u].first_seen)
                    self._items.pop(oldest, None)
                    self._dropped += 1
            payload = asdict(item)
        self._append(payload)
        return item

    def resolve(self, url: str) -> bool:
        """A later attempt succeeded — drop the URL. Returns True if it had been listed."""
        with self._lock:
            had = self._items.pop(url, None) is not None
        if had:
            self._append({"url": url, "_resolved": True, "at": _now()})
        return had

    def clear_all(self) -> int:
        """Forget every listed URL. Returns how many were dropped.

        TRUNCATES the file rather than appending a resolved-marker per URL, which is the whole
        reason this is not a loop over resolve(): the log replays on load, so N markers would make
        every future start re-read N dead lines forever. The in-memory map is cleared under the same
        lock, so a crawl running concurrently cannot repopulate from a stale view.
        """
        with self._lock:
            n = len(self._items)
            self._items.clear()
            self._dropped = 0
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text("", encoding="utf-8")
            except Exception:
                pass                  # same rule as everywhere else here: never fail a crawl
        return n

    # ── reads ────────────────────────────────────────────────────────────────
    def recent(self, limit: int = 200, cause: str | None = None, host: str | None = None) -> list[dict]:
        with self._lock:
            rows = [asdict(i) for i in self._items.values()
                    if (cause is None or i.cause == cause)
                    and (host is None or _host_key(i.host) == _host_key(host))]
        rows.sort(key=lambda r: r["last_seen"], reverse=True)
        return rows[:limit]

    def summary(self) -> dict:
        """Counts by cause and by host — what the dashboard shows above the table."""
        with self._lock:
            items = list(self._items.values())
        by_cause: dict[str, int] = {}
        by_host: dict[str, int] = {}
        for i in items:
            by_cause[i.cause] = by_cause.get(i.cause, 0) + 1
            if i.host:
                by_host[i.host] = by_host.get(i.host, 0) + 1
        return {"total": len(items), "dropped_over_cap": self._dropped,
                "by_cause": dict(sorted(by_cause.items(), key=lambda kv: -kv[1])),
                "by_host": dict(sorted(by_host.items(), key=lambda kv: -kv[1])[:25]),
                "path": str(self.path)}

    def __len__(self) -> int:
        return len(self._items)


_LOG: IssueLog | None = None
_LOG_LOCK = threading.Lock()


def log() -> IssueLog:
    """Process-wide singleton. The engine thread and the API threads share one instance."""
    global _LOG
    with _LOG_LOCK:
        if _LOG is None:
            _LOG = IssueLog()
        return _LOG


def demo() -> None:
    """python -m crawler.issues"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "iss.jsonl"
        lg = IssueLog(p)
        lg.record("http://a/1", "antibot", action="skip", evidence="cloudflare_interactive",
                  attempts=4)
        lg.record("http://a/2", "render_timeout", action="retry_later", evidence="4 renders",
                  attempts=4)
        # re-recording accumulates rather than duplicating
        lg.record("http://a/1", "antibot", attempts=6)
        assert len(lg) == 2, len(lg)
        assert lg.recent()[0]["url"] in ("http://a/1", "http://a/2")
        assert [r for r in lg.recent() if r["url"] == "http://a/1"][0]["attempts"] == 6
        assert lg.summary()["by_cause"]["antibot"] == 1
        # survives a reload from disk
        lg2 = IssueLog(p)
        assert len(lg2) == 2, len(lg2)
        # a success clears it, and the clear also survives a reload
        assert lg2.resolve("http://a/1") is True
        assert len(IssueLog(p)) == 1
        # a torn final line costs one record, not the file
        with p.open("a", encoding="utf-8") as f:
            f.write('{"url": "http://a/3", "cau')
        assert len(IssueLog(p)) == 1
    print("issues demo OK")


if __name__ == "__main__":
    demo()
