"""Send the login OTP, with no mail provider and no account anywhere.

WHY THIS IS WRITTEN OUT RATHER THAN IMPORTED. The obvious way to email a code is SendGrid/Mailgun/SES
— all of which are paid, all of which want an API key this deployment would then have to hold. The
next-most-obvious is an authenticated relay (Gmail SMTP + an app password), which is free but still
means storing a working credential for somebody's mailbox on the crawler box.

Neither is necessary, because email delivery is just SMTP: look up the recipient domain's MX and
talk to it. That is what every mail server on the internet does, it costs nothing, and it stores no
secret at all. Verified against the real target before this was written — `mssu.ac.in` is Google
Workspace, and Google accepted both recipients from this host's IP:

    MAIL FROM -> 250 2.1.0 OK
    RCPT research@mssu.ac.in     -> 250 2.1.5 OK
    RCPT 2023000058@mssu.ac.in   -> 250 2.1.5 OK

Stdlib only (`smtplib`, `socket`, `ssl`) including the MX lookup, which is a hand-rolled DNS query
because dnspython is not installed and this service's dependency list is deliberately five packages
long — one login feature is not worth a sixth.

THE HONEST CAVEAT: mail sent direct-to-MX from a host with no SPF record and no reverse DNS can be
greylisted or filed as spam, and that is outside our control. So `RELAY_HOST` still exists as an
escape hatch — set it and this delivers through a normal authenticated relay instead. It is unset by
default because the direct path is verified working and needs no credential.
"""
from __future__ import annotations

import logging
import os
import random
import smtplib
import socket
import ssl
import struct
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger("mailer")

# Envelope sender. Bounces are not read by anything — the UI reports the SMTP result synchronously —
# but a syntactically real address is what keeps receivers from rejecting on the sender alone.
FROM_ADDR = os.environ.get("DASH_MAIL_FROM", "mallory-crawler@mssu.ac.in")
FROM_NAME = os.environ.get("DASH_MAIL_FROM_NAME", "Mallory Crawler")
# EHLO name. Receivers dislike a bare hostname that is not a FQDN.
HELO = os.environ.get("DASH_MAIL_HELO", "mallory.mssu.ac.in")

# Optional authenticated relay. Unset = deliver straight to the recipient's MX (the default path).
RELAY_HOST = os.environ.get("DASH_SMTP_RELAY_HOST", "")
RELAY_PORT = int(os.environ.get("DASH_SMTP_RELAY_PORT", "587"))
RELAY_USER = os.environ.get("DASH_SMTP_RELAY_USER", "")
RELAY_PASS = os.environ.get("DASH_SMTP_RELAY_PASS", "")

TIMEOUT = float(os.environ.get("DASH_SMTP_TIMEOUT", "25"))


# ── DNS: just enough to resolve MX ────────────────────────────────────────────────────────────

def _nameservers() -> list[str]:
    """Resolvers to try. The container's own resolver first (Docker's 127.0.0.11 forwards upstream),
    then public ones — a box whose resolv.conf is unreadable should still be able to send mail."""
    out: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) > 1:
                        out.append(parts[1])
    except OSError:
        pass
    for fallback in ("8.8.8.8", "1.1.1.1"):
        if fallback not in out:
            out.append(fallback)
    return out


def _read_name(buf: bytes, i: int) -> tuple[str, int]:
    """Decode a DNS name at offset `i`, following compression pointers.

    Returns (name, offset just past the name IN THE RECORD) — a pointer is 2 bytes wide however long
    the name it expands to, so the caller's cursor must advance by 2, not by the expansion.
    """
    labels: list[str] = []
    jumped = False
    end = i
    hops = 0
    while True:
        if i >= len(buf):
            break
        n = buf[i]
        if n == 0:
            i += 1
            if not jumped:
                end = i
            break
        if n & 0xC0 == 0xC0:                      # compression pointer
            ptr = struct.unpack(">H", buf[i:i + 2])[0] & 0x3FFF
            if not jumped:
                end = i + 2
            jumped = True
            i = ptr
            hops += 1
            if hops > 20:                          # malformed/looping packet
                break
            continue
        labels.append(buf[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += n + 1
        if not jumped:
            end = i
    return ".".join(labels), end


def mx_hosts(domain: str) -> list[str]:
    """Mail exchangers for `domain`, best preference first.

    Falls back to the domain itself: an domain with no MX but an A record takes mail on its own
    address, which is what RFC 5321 says to do and what every real MTA does.
    """
    query = bytearray()
    tid = random.randint(0, 0xFFFF)
    query += struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for part in domain.encode("idna").split(b"."):
        query += bytes([len(part)]) + part
    query += b"\x00" + struct.pack(">HH", 15, 1)      # QTYPE=MX, QCLASS=IN

    for ns in _nameservers():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(5)
            sock.sendto(bytes(query), (ns, 53))
            buf, _ = sock.recvfrom(4096)
            sock.close()
        except OSError:
            continue
        if len(buf) < 12 or struct.unpack(">H", buf[:2])[0] != tid:
            continue
        qd, an = struct.unpack(">HH", buf[4:8])
        i = 12
        for _ in range(qd):                            # skip the echoed question
            _, i = _read_name(buf, i)
            i += 4
        found: list[tuple[int, str]] = []
        for _ in range(an):
            _, i = _read_name(buf, i)
            if i + 10 > len(buf):
                break
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", buf[i:i + 10])
            i += 10
            if rtype == 15 and rdlen >= 3:
                pref = struct.unpack(">H", buf[i:i + 2])[0]
                name, _ = _read_name(buf, i + 2)
                if name:
                    found.append((pref, name))
            i += rdlen
        if found:
            return [h for _, h in sorted(found)]
    return [domain]


# ── delivery ──────────────────────────────────────────────────────────────────────────────────

def _build(to_addr: str, subject: str, text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{FROM_ADDR}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=HELO)
    # A one-time code is worthless once used and must never sit in a proxy cache or an archive.
    msg["X-Auto-Response-Suppress"] = "All"
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    return msg


def _deliver_direct(to_addr: str, msg: EmailMessage) -> None:
    domain = to_addr.rsplit("@", 1)[-1]
    errors = []
    for host in mx_hosts(domain)[:3]:
        try:
            smtp = smtplib.SMTP(host, 25, timeout=TIMEOUT, local_hostname=HELO)
            try:
                smtp.ehlo(HELO)
                if smtp.has_extn("starttls"):
                    # Opportunistic: an unverified certificate is still better than plaintext, and
                    # refusing to send because a receiver's cert does not chain would just mean the
                    # operator never gets their code.
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    smtp.starttls(context=ctx)
                    smtp.ehlo(HELO)
                smtp.send_message(msg, from_addr=FROM_ADDR, to_addrs=[to_addr])
                return
            finally:
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001 — QUIT failing does not un-send the message
                    pass
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {e}")
    raise RuntimeError("; ".join(errors) or f"no MX for {domain}")


def _deliver_relay(to_addr: str, msg: EmailMessage) -> None:
    smtp = smtplib.SMTP(RELAY_HOST, RELAY_PORT, timeout=TIMEOUT, local_hostname=HELO)
    try:
        smtp.ehlo(HELO)
        if smtp.has_extn("starttls"):
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo(HELO)
        if RELAY_USER:
            smtp.login(RELAY_USER, RELAY_PASS)
        smtp.send_message(msg, from_addr=FROM_ADDR, to_addrs=[to_addr])
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001
            pass


def send(recipients: list[str], subject: str, text: str) -> dict[str, str | None]:
    """Send one message to each recipient. Returns {address: None on success, else the error}.

    PER RECIPIENT, not one message with several To: headers — the two addresses on this account are
    the SAME second factor, and a single failed RCPT must not deny the code to the address that was
    reachable. The caller decides what "delivered" means; this only reports.
    """
    out: dict[str, str | None] = {}
    for addr in recipients:
        addr = addr.strip()
        if not addr:
            continue
        try:
            msg = _build(addr, subject, text)
            if RELAY_HOST:
                _deliver_relay(addr, msg)
            else:
                _deliver_direct(addr, msg)
            out[addr] = None
            log.info("otp mail delivered to %s", mask(addr))
        except Exception as e:  # noqa: BLE001
            out[addr] = str(e)[:300]
            log.warning("otp mail FAILED to %s: %s", mask(addr), e)
    return out


def mask(addr: str) -> str:
    """`research@mssu.ac.in` -> `re****@mssu.ac.in`. Enough for the operator to recognise which
    mailbox to open, not enough for a stranger at the login screen to harvest an address."""
    user, _, dom = addr.partition("@")
    if not dom:
        return "****"
    head = user[:2] if len(user) > 3 else user[:1]
    return f"{head}{'*' * max(3, len(user) - len(head))}@{dom}"
