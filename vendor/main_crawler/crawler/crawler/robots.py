"""robots.txt politeness (honors source_registry → respect_robots_txt).

A small per-host cache of parsed robots rules. Live fetches consult it before
hitting a URL; a disallowed URL is skipped (never fetched). Fixtures bypass this
entirely — they are our own offline test content, not live sites.

Fail-open by default: if robots.txt is ABSENT (4xx / empty) we allow (absence of
rules = permitted, per the standard). But we distinguish "absent" from "couldn't
read it" (5xx / timeout / network error): for a CAREFUL host (.gov/.mil), an
UNKNOWN robots.txt means we can't confirm permission, so we DON'T fetch —
fail-CLOSED (gated by CRAWLER_ROBOTS_STRICT_CAREFUL, default on). This is the
polite/legitimate posture, never an evasion.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from . import errors

# reasons stored alongside a None parser
_OK = "ok"
_NO_ROBOTS = "no_robots"      # 4xx / empty → no rules → allowed
_FETCH_ERROR = "fetch_error"  # 5xx / timeout / network → unknown


def _strict_careful() -> bool:
    return os.environ.get("CRAWLER_ROBOTS_STRICT_CAREFUL", "1") == "1"


class RobotsCache:
    def __init__(self, user_agent: str, timeout_s: int = 10):
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[RobotFileParser | None, str]] = {}

    def _parser_for(self, url: str) -> tuple[RobotFileParser | None, str]:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return None, _NO_ROBOTS
        if host in self._cache:
            return self._cache[host]
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        rp: RobotFileParser | None = RobotFileParser()
        rp.set_url(robots_url)
        reason = _OK
        try:
            import httpx
            resp = httpx.get(robots_url, timeout=self.timeout_s,
                            headers={"User-Agent": self.user_agent},
                            follow_redirects=True)
            sc = resp.status_code
            if sc >= 500:
                rp, reason = None, _FETCH_ERROR       # server error → couldn't read → unknown
            elif sc >= 400 or not resp.text.strip():
                rp, reason = None, _NO_ROBOTS          # 4xx / empty → no rules → allowed
            else:
                rp.parse(resp.text.splitlines())
        except Exception:
            rp, reason = None, _FETCH_ERROR            # timeout / unreachable → unknown
        self._cache[host] = (rp, reason)
        return self._cache[host]

    def _deny_on_unknown(self, url: str, reason: str) -> bool:
        """A careful host with an UNKNOWN robots.txt → don't fetch (can't confirm permission)."""
        if reason != _FETCH_ERROR or not _strict_careful():
            return False
        host = (urlsplit(url).hostname or "").lower()
        return errors.is_careful_host(host)

    def allowed(self, url: str) -> bool:
        rp, reason = self._parser_for(url)
        if rp is None:
            return not self._deny_on_unknown(url, reason)   # no_robots→allow; unknown+gov→deny
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def decision(self, url: str) -> str:
        """For the audit trail: 'allow' | 'deny' | 'no_robots'."""
        rp, reason = self._parser_for(url)
        if rp is None:
            if reason == _NO_ROBOTS:
                return "no_robots"
            return "deny" if self._deny_on_unknown(url, reason) else "allow"
        try:
            return "allow" if rp.can_fetch(self.user_agent, url) else "deny"
        except Exception:
            return "allow"

    def crawl_delay(self, url: str) -> float | None:
        rp, _ = self._parser_for(url)
        if rp is None:
            return None
        try:
            d = rp.crawl_delay(self.user_agent)
            return float(d) if d is not None else None
        except Exception:
            return None
