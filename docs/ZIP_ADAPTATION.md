# Production crawler ZIP: access/discovery adaptation

Source: user-supplied `C:\Users\vijay\Desktop\Crawler.zip`, reviewed read-only. The archive was not executed or installed, and its embedded documents are reference material rather than instructions. This is a targeted adaptation into CrawlScope, not a drop-in import of the production engine.

## Reviewed mechanisms

| ZIP source | CrawlScope implementation |
| --- | --- |
| `crawler/robots.py` | Distinguish unavailable robots 4xx from unreachable network/5xx; honor downloaded rules. Keep 429 conservative. |
| `crawler/captcha.py` HTTP detection | Structural/header-based challenge detection, distinguishing an embedded widget/vendor mention from withheld content; provider and access reason in events. |
| `crawler/fetcher.py` HTTP-to-browser fallback | Normal browser rendering and bounded wait for ordinary JavaScript verification, validating actual HTTP status/content afterwards. |
| `crawler/fetcher.py` protected fetch | Share browser cookies with deeper HTTP HTML discovery and use same-origin referrers. Resource downloads are intentionally not ported: file/image/document URLs are collected only. |
| `crawler/siteauth.py` session reuse concept | Public-cookie session reuse only, with exact-origin saved-session selection, expired-cookie removal and current-user Windows encryption. No account creation or login execution. |
| `crawler/fetcher.py` / `docs/ERROR_HANDLING.md` | Shared host cooldowns, numeric/date Retry-After handling, adaptive pacing, bounded retries and wall-clock HTTP timeout. |
| Published pagination/discovery mechanisms | Existing sitemap indexes, static links, feeds, JSON/API links, browser controls, frames, Load More and deep pagination remain independent discovery sources. |

## Not imported

- Automated/paid CAPTCHA solving, OCR challenge solving or token injection.
- Fingerprint masking, proxy/IP rotation, stealth-vendor services or TLS-verification bypass.
- Automatic disposable-account registration, credential storage, login attempts or paywall access.
- Production ingestion, keyword relevance gates, production fixed depth/page limits or fleet scaling. These are a different workflow; copying its exclusion gates into a coverage auditor could reproduce the same omissions that the auditor is intended to detect.
- Synthetic fixture content as substitutes for live website responses.

The ZIP contains a substantial production engine, multiple services and benchmark/checklist documents. There is no verified one-to-one 116-feature manifest in the reviewed material; neither complete feature parity nor unrestricted access is claimed. The ZIP's own edge-case report distinguishes tested, failed, partial and untestable cases, including auth/geo/CAPTCHA limitations.

## Verification scope

Local controlled fixtures cover robots HTTP 403 versus actual Disallow/5xx/429, normal security prose/widget pages, CAPTCHA/login walls, browser cookie recovery, URL-only resource discovery, cookie scoping/encryption and Retry-After sharing. Discovery remains isolated from corpus verification. Only HTML text is compared; no document/image/file content or Corpus file text is retrieved. No production target or Corpus load test is performed as part of this port.
