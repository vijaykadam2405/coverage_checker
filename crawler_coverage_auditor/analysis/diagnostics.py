"""Explain saved failures without erasing history or disguising website blocks."""
from collections import defaultdict


def issue_info(stage: str, message: str, *, readable: bool = False, url: str | None = None) -> tuple[str, str, str]:
    text = message.lower()
    if stage == 'Historical discovery retry':
        return ('Recovered discovery source', 'Historical', 'This source was successfully retried. The previous error remains as history; other discovery boundaries still apply.')
    if stage == 'Historical pause':
        return ('Saved pause checkpoint', 'Historical', 'The scan continued from its saved checkpoint and exhausted its pending work. This pause is retained as history, not a current discovery boundary. Other website/Corpus errors still apply.')
    if stage == 'Historical count cutoff':
        return ('Recovered historical discovery cutoff', 'Historical', 'The old count cutoff was followed by an uncapped rediscovery pass. Its original evidence is retained; this is not a current count limit or proof that every website page exists in the inventory.')
    if stage == 'Document text':
        return ('Historical document extraction', 'Historical', 'No longer performed: files are URL-only. Keep this record as scan history; no OCR/decryption is required.')
    if 'javascript error:' in text and readable:
        return ('Website script warning; HTML readable', 'Diagnostic', 'The website script failed, but HTML was captured. Current code records this as a script note, not proof of a blocked page.')
    if stage.lower().startswith('corpus') or 'corpus api' in text:
        if 'certificate' in text or 'ssl_error' in text or 'tls' in text:
            return ('Corpus connection failure', 'Verification',
                    'Retry the saved scan using the current API connection. No certificate setup is required. If the connection still fails, check the API address, HTTPS port and server availability. Saved successful checks are retained; incomplete checks stay unverified.')
        return ('Corpus database / service unavailable' if any(t in text for t in ('timed out', 'unreachable', 'unavailable', '503', '429', 'timeout')) else 'Corpus verification needs review',
                'Verification', 'Checks remain unverified. Retry with paced recovery; persistent failures require Corpus database/server investigation, not a missing-page label.')
    if 'too_large' in text or 'decoded response limit' in text:
        return ('Website response exceeds safety limit', 'Discovery', 'The partial body was not accepted as complete content. Inspect the URL and declared size; discovery remains incomplete here. Files continue to be URL-only.')
    if 'redirect_loop' in text or 'maximum allowed redirects' in text:
        return ('Redirect loop / hop limit', 'Discovery', 'The redirect chain was stopped without repeatedly following it. Check the website redirect configuration; the URL is not proven absent from Corpus.')
    if 'ssl_error' in text:
        return ('Website certificate verification failed', 'Discovery', 'TLS verification remains enabled. Check the site certificate chain or local trust configuration; no insecure connection was attempted.')
    if any(t in text for t in ('name_not_resolved', 'getaddrinfo', 'dns')):
        return ('DNS resolution failed', 'Discovery', 'Bounded navigation retries are used. If persistent, check DNS/network availability; the URL is not proven absent.')
    if '404' in text:
        if url and url != url.strip():
            return ('Historical untrimmed URL / 404', 'Historical', 'This scan requested a URL with surrounding whitespace. Current code trims it before deduplication and fetching. Rescan to validate corrected URLs; old records are retained.')
        return ('Website link returned 404', 'Website', 'Excluded from valid HTML coverage. The website returned a missing page; CrawlScope cannot repair its broken link.')
    if any(t in text for t in ('403', 'captcha', 'bot_', 'login', 'robots', 'paywall', 'consent_', 'age_verification')):
        return ('Website access restriction', 'Discovery', 'Respect access rules. Use an authorized public browser session where applicable; blocked content remains unverified.')
    if '429' in text or 'rate limit' in text:
        return ('Website rate limit', 'Discovery', 'Honor Retry-After and reduce website request frequency. Other permitted URLs may continue; the blocked boundary remains unknown.')
    if any(t in text for t in ('timeout', 'timed out', 'connection', 'ssl', 'tls', '5xx', '503', '502', '500')):
        return ('Website request failed', 'Discovery', 'Bounded retry/backoff is used. Check network or website availability; a failed request does not prove that content is absent.')
    if 'more-results control' in text or 'intercepts pointer events' in text:
        return ('Page control / overlay prevented expansion', 'Discovery', 'Current code closes known media previews before expansion. Other blocked controls remain disclosed as incomplete exploration.')
    if 'javascript_diagnostic' in stage.lower():
        return ('Website script note', 'Diagnostic', 'Read the URL evidence to distinguish readable HTML from failed rendering.')
    return ('Exploration or verification boundary', 'Discovery', 'Inspect the URL and original message below; this boundary may conceal additional content.')


def diagnostic_summary(items: list[dict], rows: list[dict]) -> list[dict]:
    readable = {row['url'] for row in rows if row.get('page_type') == 'HTML' and row.get('validity') == 'VALID_PAGE'}
    groups = defaultdict(list)
    for item in items:
        message = item.get('reason') or item.get('message') or ''
        info = issue_info(item.get('stage', ''), message, readable=item.get('url') in readable, url=item.get('url'))
        groups[info].append(item)
    return [{'Issue': kind, 'Impact': impact, 'Records': len(group), 'Affected URLs': len({x.get('url') for x in group if x.get('url')}),
             'Example URL': next((x['url'] for x in group if x.get('url')), None),
             'Explanation / next step': action} for (kind, impact, action), group in groups.items()]


def split_boundaries(items: list[dict], rows: list[dict]) -> tuple[list[dict], list[dict]]:
    readable = {row['url'] for row in rows if row.get('page_type') == 'HTML' and row.get('validity') == 'VALID_PAGE'}
    boundaries, notes = [], []
    for item in items:
        _, impact, _ = issue_info(item.get('stage', ''), item.get('reason') or item.get('message') or '', readable=item.get('url') in readable, url=item.get('url'))
        (notes if impact in {'Historical', 'Diagnostic'} else boundaries).append(item)
    return boundaries, notes
