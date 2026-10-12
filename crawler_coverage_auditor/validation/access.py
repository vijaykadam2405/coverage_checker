"""Classify access walls without mistaking security-related articles for challenges.

Adapted from the ZIP's distinction between definitive challenge markers and
ordinary vendor/widget presence. This module detects restrictions; it does not
solve challenges, submit login forms, or manufacture successful content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass(frozen=True, slots=True)
class AccessIssue:
    status: str
    reason: str
    provider: str | None = None
    browser_wait: bool = False


def detect_access(status: int | None, headers: dict, html: str, url: str = "") -> AccessIssue | None:
    headers = {str(key).lower(): str(value).lower() for key, value in headers.items()}
    if status == 429:
        return AccessIssue("RATE_LIMITED", "HTTP 429: website rate limited requests; respect Retry-After and pause this host")
    if status == 451:
        return AccessIssue("GEO_BLOCKED", "HTTP 451: content unavailable for legal/regional reasons")
    if status == 401:
        return AccessIssue("LOGIN_REQUIRED", "HTTP 401: authentication required; public-page audit does not attempt login")
    soup = BeautifulSoup(html[:500_000], "lxml")
    for node in soup.select("[hidden], [aria-hidden='true']"):
        node.decompose()
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    heading = " ".join(item.get_text(" ", strip=True) for item in soup.select("h1, h2")).lower()
    scripts = " ".join(str(node.get('src', '')) + ' ' + node.get_text()
                       for node in soup.select('script') if node.get('type', '').lower() in {'', 'text/javascript', 'application/javascript', 'module'}).lower()
    for node in soup.select("script, style, template, noscript"):
        node.decompose()
    visible = soup.get_text(" ", strip=True).lower()
    raw = html[:500_000].lower()
    thin = len(visible) < 1500
    challenge_title = any(phrase in title + " " + heading for phrase in (
        "just a moment", "verify you are human", "security check", "robot check",
        "checking your browser", "access denied", "attention required", "captcha verification"))
    cf = (headers.get("cf-mitigated") == "challenge" or "cf_chl_opt" in scripts
          or soup.select_one('#cf-browser-verification') is not None
          or (status in {403, 503} and "just a moment" in title and "cloudflare" in raw))
    if cf:
        interactive = bool(re.search(r"ctype\s*[:=]\s*['\"]interactive", raw))
        return AccessIssue("CAPTCHA_REQUIRED" if interactive else "BOT_BLOCKED",
            "Cloudflare challenge: user action needed" if interactive else "Cloudflare browser verification has not completed",
            "cloudflare", not interactive)
    action = headers.get("x-amzn-waf-action", "")
    if action in {"captcha", "challenge", "block"}:
        return AccessIssue("CAPTCHA_REQUIRED" if action == "captcha" else "BOT_BLOCKED",
            f"AWS WAF {action}: website withheld content", "aws_waf", action == "challenge")
    providers = {
        "servicepipe": ("servicepipe.ru/loaders/", "<js-challenge-loader", "id_captcha_frame_div"),
        "anubis": ('id="anubis_challenge"', "id='anubis_challenge'"),
        "datadome": ("ct.captcha-delivery.com", "geo.captcha-delivery.com"),
        "perimeterx": ("window._pxappid", "_pxaction"),
        "akamai": ("errors.edgesuite.net", 'page_owner": "akamai"'),
        "imperva": ("_incapsula_resource",),
        "kasada": ("149e9513-01fa-4fb0-aad4-566afd725d1b",),
    }
    for provider, markers in providers.items():
        structural = (provider == 'servicepipe' and soup.select_one('js-challenge-loader, #id_captcha_frame_div') is not None
                      or provider == 'anubis' and soup.select_one('#anubis_challenge') is not None)
        if structural or (any(marker in raw for marker in markers) and status in {403, 406, 456, 503, 999}):
            return AccessIssue("BOT_BLOCKED", f"{provider} access challenge: content withheld", provider,
                               provider in {"servicepipe", "anubis"})
    widget = soup.select_one(".g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*='recaptcha'], iframe[src*='hcaptcha']")
    # A contact form's embedded CAPTCHA is not a content-blocking interstitial.
    if widget and thin and challenge_title:
        return AccessIssue("CAPTCHA_REQUIRED", "Interactive CAPTCHA: complete it yourself in the session browser; no automatic solving")
    login_heading = any(re.search(r"^(log[ -]?in|sign[ -]?in|authentication required)(\b|$)", item)
                        for item in (title.strip(), heading.strip()))
    if soup.select_one("input[type=password]") and (login_heading or (thin and re.search(r"/(login|signin|sign-in)(/|\?|$)", url))):
        return AccessIssue("LOGIN_REQUIRED", "Login page: excluded from the public-page audit; no registration or login attempted")
    if status in {403, 406, 456, 999} or (thin and challenge_title):
        return AccessIssue("BLOCKED", f"Website denied access (HTTP {status or 'unknown'}); browser/session or owner allowlisting may be required")
    return None
