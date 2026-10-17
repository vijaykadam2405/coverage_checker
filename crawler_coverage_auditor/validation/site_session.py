"""User-created public browser sessions, isolated by origin and never exported.

Windows sessions are encrypted for the current OS user using DPAPI. On other
platforms they are local mode-0600 files. No passwords or external solver calls.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import tempfile
import time
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urlsplit


def origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Enter an HTTP(S) website URL without embedded credentials")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def scoped_state(state: dict, url: str) -> dict:
    host = urlsplit(url).hostname.lower()
    cookies = []
    for cookie in state.get("cookies", []):
        domain = str(cookie.get("domain", "")).lower()
        bare = domain.lstrip(".")
        if not bare or not (host == bare or (domain.startswith(".") and host.endswith("." + bare))):
            continue
        expiry = cookie.get("expires", -1)
        if expiry != -1 and expiry <= time.time():
            continue
        cookies.append({key: cookie[key] for key in (
            "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if key in cookie})
    # Public challenge sessions need cookies only, not login tokens, passkeys or localStorage.
    return {"cookies": cookies, "origins": []}


def _crypt(payload: bytes, *, decrypt: bool = False) -> bytes:
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("Local browser session encryption/decryption failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class SiteSessionStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path(self, url: str) -> Path:
        key = hashlib.sha256(origin(url).encode()).hexdigest()
        return self.directory / (key + ".session")

    def save(self, url: str, state: dict, user_agent: str) -> Path:
        record = {"origin": origin(url), "created": time.time(), "user_agent": user_agent,
                  "state": scoped_state(state, url)}
        payload = json.dumps(record).encode()
        payload = b"DPAPI\n" + _crypt(payload) if sys.platform == "win32" else b"LOCAL\n" + payload
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(url)
        # Only deliberate session preparation persists secrets; ordinary scans do not.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                if sys.platform != 'win32':
                    os.chmod(temporary, 0o600)
                stream.write(payload)
            temporary.replace(path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return path

    def load(self, url: str) -> dict | None:
        path = self.path(url)
        if not path.is_file():
            return None
        payload = path.read_bytes()
        if payload.startswith(b"DPAPI\n") and sys.platform == "win32":
            payload = _crypt(payload[6:], decrypt=True)
        elif payload.startswith(b"LOCAL\n") and sys.platform != "win32":
            payload = payload[6:]
        else:
            raise ValueError("Session belongs to a different OS/user; prepare a fresh session")
        record = json.loads(payload)
        if record["origin"] != origin(url):
            raise ValueError("Session origin does not match this website")
        if time.time() - record["created"] > 24 * 3600:
            return None
        record["state"] = scoped_state(record["state"], url)
        return record


def import_cookies(client, cookies: list[dict], url: str) -> None:
    """Keep expiry, path and Secure semantics; never set a global Cookie header."""
    for item in scoped_state({"cookies": cookies}, url)["cookies"]:
        domain = item["domain"]
        expiry = item.get("expires", -1)
        client.cookies.jar.set_cookie(Cookie(
            version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
            path=item.get("path", "/"), path_specified=True, secure=item.get("secure", False),
            expires=None if expiry == -1 else int(expiry), discard=expiry == -1,
            comment=None, comment_url=None, rest={"HttpOnly": item.get("httpOnly", False)}, rfc2109=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a public website session yourself; no automated CAPTCHA/login")
    parser.add_argument("--url", required=True)
    parser.add_argument("--directory", type=Path, default=Path(os.environ.get("CRAWLSCOPE_SESSION_DIR",
        str(Path(os.environ.get("CRAWLSCOPE_DATA_DIR", "data")) / "sessions"))))
    args = parser.parse_args()
    origin(args.url)
    from crawler_coverage_auditor.validation.network_safety import validate_target
    validate_target(args.url, resolve=True)
    from playwright.sync_api import sync_playwright

    from crawler_coverage_auditor.validation.access import detect_access
    print("A separate browser will open. Complete any public-page CAPTCHA yourself.")
    print("Do not sign in, register, accept subscriptions or bypass certificate warnings.")
    print("When the public content is visible, return here and press Enter to save its cookies.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        try:
            context = browser.new_context(ignore_https_errors=False, accept_downloads=False)
            def restrict(route):
                try:
                    validate_target(route.request.url, resolve=True)
                except ValueError:
                    route.abort()
                    return
                route.continue_()
            context.route('**/*', restrict)
            page = context.new_page()
            latest = [None]
            def record(response):
                if response.request.resource_type == "document" and response.request.frame == page.main_frame:
                    latest[0] = response
            page.on("response", record)
            page.goto(args.url, wait_until="domcontentloaded")
            input("Press Enter only after the public page is accessible (Ctrl+C cancels): ")
            if origin(page.url) != origin(args.url):
                raise ValueError("Browser left the requested origin; no session saved")
            response = latest[0]
            issue = detect_access(response.status if response else None,
                response.headers if response else {}, page.content(), page.url)
            if issue or not response or response.status != 200:
                raise ValueError(issue.reason if issue else "No successful HTTP 200 content response; no session saved")
            path = SiteSessionStore(args.directory).save(args.url, context.storage_state(),
                                                       page.evaluate("navigator.userAgent"))
            print(f"Local public-session cookies saved: {path}. Start a new discovery scan.")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
