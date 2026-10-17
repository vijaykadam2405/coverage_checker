# Standalone CrawlScope browser

CrawlScope and the production crawler are separate applications. The only
production integration is the read-only Corpus coverage API. CrawlScope never
uses the production crawler's CamoFox REST service, fleet, storage or sessions.

## What was adapted

The supplied main crawler has `crawler/crawler/camofox_client.py`. That client
calls a separate CamoFox server, which launches **Camoufox**, a Firefox-based
browser, with Playwright. Its server launch choices include headless operation,
host-appropriate browser identity, humanized browser input and excluding the
uBlock addon to avoid extra downloads at launch.

CrawlScope uses those browser-engine choices locally through Camoufox's Python
interface. It does **not** call the imported REST client or start the imported
server. Existing Playwright discovery, filters, tabs, pagination and HTML text
comparison stay in CrawlScope. No proxy rotation, account creation, Buster
extension, paid CAPTCHA task or document extraction is enabled.

## Install on Windows

From the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-browser.txt
.\.venv\Scripts\python.exe scripts/setup_local_browser.py
```

The setup script installs the pinned official Windows/browser asset under
`data/browser/camoufox/`. This folder contains software, not scan inventory.
It does not replace the machine-wide Camoufox installation. The binary release
is pinned in the installer for reproducibility; evaluate newer releases before
changing that pin. The Python dependency is pinned and included in the normal
project requirements. Camoufox is the default local engine, not a Control Room opt-in.

Do **not** use `python -m camoufox fetch` for this project: that library version's
default command updates a shared machine cache and installs optional addons.
Runtime initialization never downloads a missing browser. It reports an error
instead, so scans cannot unexpectedly run the installer.

Backend-only `.env` setting:

```dotenv
CRAWLSCOPE_BROWSER_ENGINE=camoufox
```

Use `chromium` to select the original browser. `CAMOFOX_ENABLED`, `CAMOFOX_URL`,
`CAMOFOX_API_KEY` and your friend's browser credentials are not used by this
adapter. A browser identity script intended for Chromium is not injected into
Camoufox. Existing public session cookies remain local to CrawlScope; Chromium
user-agent overrides are not applied to Firefox.

Pause an active scan and wait for checkpoint confirmation before restarting.
Stop the existing dashboard with Ctrl+C in its terminal, then run:

```powershell
npm.cmd --prefix frontend start
```

No extra browser service or listening port is created. Activity records the
selected local engine. Old blocked results are historical evidence: they are not
retroactively changed to successful pages. Start a new scan to retest a completed
blocked inventory; restarting alone does not refetch its completed URLs.

## Preserved protections and resource rules

- Website TLS validation, public-network checks, robots rules, pacing and server
  cooldowns remain active. Corpus calls stay read-only and sequential.
- Images/media/fonts and recognized file URLs are blocked before browser fetch.
  PDFs/documents are inventoried and checked for URL presence only.
- Chromium still uses CDP response-stage interception for extensionless files.
  Firefox does not support CDP. Camoufox instead sends a paced, body-free HEAD
  request before document/XHR/fetch requests. File MIME types and attachments
  become URL-only links without a subsequent GET. Unsupported or uncertain
  HEAD results are blocked and recorded as coverage limitations.
- Redirects are checked again as subsequent browser requests. Non-read-only
  requests are not sent by the Camoufox adapter. Unexpected browser downloads
  are cancelled; no file text is extracted or saved.
- A server can return different MIME types for HEAD and GET. HEAD is therefore
  not a universal proof of resource type; download cancellation is a secondary
  safeguard. This browser mode cannot promise that zero file bytes ever arrive
  from a mislabelled/changing response. It never intentionally downloads files
  or uses their contents for text coverage.
- HEAD checks add website requests, not Corpus requests. They are paced and
  serial. A website that rejects HEAD may require Chromium or owner support.
- CAPTCHA/login/remaining blocks are reported, not counted as valid HTML or
  confirmed misses. A local browser cannot guarantee access or complete coverage.

## Implementation

| File | Responsibility |
| --- | --- |
| `discovery/local_browser.py` | Local engine selection, isolated cache and launch options |
| `discovery/browser.py` | Shared discovery, routing, resource guard, activity, cleanup |
| `scripts/setup_local_browser.py` | Explicit official-software install into project data |
| `requirements-browser.txt` | Compatibility alias for the normal project requirements |
| `tests/test_local_browser.py` | Service isolation, header/body safety and local fixture smoke test |

Official references: [Camoufox Python usage](https://camoufox.com/python/usage/)
and [installation](https://camoufox.com/python/installation/). These describe the
underlying engine; use this project's isolated installer rather than their
shared-cache fetch command for this checkout.
