"""Disposable inboxes, for confirming a registration on a portal that gates its data behind a
free account.

Two providers, chosen by ``CRAWLER_TEMPMAIL_PROVIDER``:

``mailtm`` (default)
    The public mail.tm API. No key, no infrastructure, no DNS — it owns the domains and the MX
    records, we just ask it for an address. This is why it is the default.

``mailhog``
    A self-hosted MailHog/MailDev container (``CRAWLER_TEMPMAIL_URL``, default
    ``http://localhost:8025``), read over its REST API.

    **Read this before choosing it.** A container cannot receive internet mail on its own. SMTP
    delivery is driven by the recipient DOMAIN's MX record, so the portal's confirmation mail
    reaches your MailHog only if you own a domain and point its MX at the host running it, on
    port 25, reachable from the public internet. Without that the container starts fine, the
    address looks valid, and every confirmation mail is silently delivered somewhere else — a
    failure that looks exactly like "the site never sent it". Own a domain and want inboxes
    nobody else can read? Use this. Otherwise use mailtm.

Both providers expose the same two things: an address, and ``wait_for_link``.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import string
import time

log = logging.getLogger("tempmail")

_MAILTM = "https://api.mail.tm"
_POLL_S = 3.0

# Confirmation links are the point of this module. Ordered most-specific first so a mail that
# contains both a verify link and an unsubscribe link yields the verify one.
_LINK_HINTS = ("verify", "confirm", "activate", "activation", "validate", "/auth/", "token=")
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.I)


class TempMailError(RuntimeError):
    """The inbox provider itself failed — distinct from "the mail never arrived"."""


def random_password(n: int = 16) -> str:
    """A password we will never memorise but must satisfy arbitrary portal rules — so guarantee
    one of each class rather than hoping 16 random characters happen to contain a digit."""
    pools = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%&*")
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice("".join(pools)) for _ in range(max(0, n - len(pools)))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def extract_link(body: str, hints: tuple[str, ...] = _LINK_HINTS) -> str | None:
    """Best confirmation URL in an email body (HTML or plain text).

    Prefers an <a href> whose URL contains a hint word, then any URL containing one, then — only
    if the mail carries exactly one URL — that URL. The last rule is deliberate: several portals
    send a bare tokenised link with no hint word in it, but a mail with five URLs and no hint is
    a newsletter, and guessing on it would click something unrelated."""
    if not body:
        return None
    hrefs = _HREF_RE.findall(body)
    urls = hrefs + [u for u in _URL_RE.findall(body) if u not in hrefs]
    urls = [u.replace("&amp;", "&").rstrip(".,)") for u in urls if u.lower().startswith("http")]
    for u in urls:
        if any(h in u.lower() for h in hints):
            return u
    return urls[0] if len(urls) == 1 else None


class Inbox:
    """One disposable mailbox. ``address`` is what you type into the registration form."""

    def __init__(self, address: str, password: str = ""):
        self.address = address
        self.password = password

    def messages(self) -> list[str]:            # pragma: no cover - provider-specific
        """Every message body currently in the box, newest first."""
        raise NotImplementedError

    def wait_for_link(self, timeout_s: float = 120.0,
                      hints: tuple[str, ...] = _LINK_HINTS) -> str | None:
        """Poll until a message carrying a confirmation link arrives. None on timeout — a portal
        that sends no confirmation mail is a normal outcome (many only need the address to be
        syntactically real), so this is not an exception."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                for body in self.messages():
                    link = extract_link(body, hints)
                    if link:
                        return link
            except TempMailError:
                raise
            except Exception as exc:
                log.debug("inbox poll failed (retrying): %s", exc)
            time.sleep(_POLL_S)
        return None


class MailTmInbox(Inbox):
    def __init__(self, address: str, password: str, token: str):
        super().__init__(address, password)
        self._token = token

    def messages(self) -> list[str]:
        import httpx
        h = {"Authorization": f"Bearer {self._token}"}
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{_MAILTM}/messages", headers=h)
            if r.status_code == 429:
                raise RuntimeError("mail.tm rate limit")   # retryable, not a TempMailError
            r.raise_for_status()
            out = []
            for m in r.json().get("hydra:member", []):
                d = c.get(f"{_MAILTM}/messages/{m['id']}", headers=h)
                if d.status_code != 200:
                    continue
                full = d.json()
                html = full.get("html") or []
                body = "\n".join(html) if isinstance(html, list) else str(html)
                # Fall back to the plain-text part: a mail with an EMPTY html list still carries
                # the link, and `"".join([])` would otherwise hand back an empty body forever.
                out.append(body or full.get("text", "") or "")
            return out


class MailHogInbox(Inbox):
    def __init__(self, address: str, base_url: str):
        super().__init__(address, "")
        self._base = base_url.rstrip("/")

    def messages(self) -> list[str]:
        import httpx
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{self._base}/api/v2/messages", params={"limit": 50})
            r.raise_for_status()
            out = []
            for m in r.json().get("items", []):
                # MailHog stores the raw MIME body; good enough for link extraction, and parsing
                # multipart properly would buy us nothing a regex over the whole blob doesn't.
                to = ",".join(f"{p.get('Mailbox')}@{p.get('Domain')}" for p in (m.get("To") or []))
                if self.address.lower() not in to.lower():
                    continue
                out.append((m.get("Content") or {}).get("Body", ""))
            return out


def _mailtm_inbox() -> Inbox:
    import httpx
    pw = random_password()
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_MAILTM}/domains")
        if r.status_code != 200:
            raise TempMailError(f"mail.tm domains: HTTP {r.status_code}")
        domains = [d["domain"] for d in r.json().get("hydra:member", []) if d.get("domain")]
        if not domains:
            raise TempMailError("mail.tm returned no usable domains")
        addr = f"{secrets.token_hex(6)}@{domains[0]}"
        r = c.post(f"{_MAILTM}/accounts", json={"address": addr, "password": pw})
        if r.status_code not in (200, 201):
            raise TempMailError(f"mail.tm account create: HTTP {r.status_code} {r.text[:200]}")
        r = c.post(f"{_MAILTM}/token", json={"address": addr, "password": pw})
        if r.status_code not in (200, 201):
            raise TempMailError(f"mail.tm token: HTTP {r.status_code}")
        token = r.json().get("token")
        if not token:
            raise TempMailError("mail.tm issued no token")
    log.info("disposable inbox ready: %s", addr)
    return MailTmInbox(addr, pw, token)


def _mailhog_inbox() -> Inbox:
    base = os.environ.get("CRAWLER_TEMPMAIL_URL", "http://localhost:8025")
    domain = os.environ.get("CRAWLER_TEMPMAIL_DOMAIN", "")
    if not domain:
        raise TempMailError(
            "CRAWLER_TEMPMAIL_DOMAIN is required for the mailhog provider: a container can only "
            "receive a portal's confirmation mail if you own a domain whose MX record points at "
            "it. Set it to that domain, or use CRAWLER_TEMPMAIL_PROVIDER=mailtm.")
    addr = f"{secrets.token_hex(6)}@{domain}"
    log.info("mailhog inbox: %s (via %s)", addr, base)
    return MailHogInbox(addr, base)


def new_inbox() -> Inbox:
    """A fresh disposable inbox from the configured provider."""
    provider = os.environ.get("CRAWLER_TEMPMAIL_PROVIDER", "mailtm").strip().lower()
    if provider == "mailhog":
        return _mailhog_inbox()
    if provider != "mailtm":
        raise TempMailError(f"unknown CRAWLER_TEMPMAIL_PROVIDER={provider!r} (mailtm|mailhog)")
    return _mailtm_inbox()
