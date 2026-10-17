"""One public translation for independent website, Corpus and HTML-text states."""
import json
import math

from crawler_coverage_auditor.analysis.coverage import is_found, is_inventory_url
from crawler_coverage_auditor.analysis.dashboard import TEXT_LABELS
from crawler_coverage_auditor.validation.text_coverage import ASSESSED_TEXT_STATUSES

RUN_LABELS = {'AWAITING_CORPUS': 'Saved inventory', 'CORPUS_PAUSED': 'Scan paused',
    'RUNNING': 'Running', 'COMPLETED': 'Completed', 'LIMITED': 'Completed · limited exploration',
    'CANCELLED': 'Scan paused by user', 'FAILED': 'Needs attention'}

LABELS = {
    'discovered': ('Discovered', 'pending', 'Awaiting website validation'),
    'fetching': ('Crawling', 'processing', 'Fetching public HTML'),
    'rendering': ('Rendering', 'processing', 'Inspecting browser content'),
    'validated': ('Collected successfully', 'found', 'Website content validated'),
    'collected': ('URL collected', 'pending', 'File URL only; availability not tested'),
    'failed': ('Failed / excluded', 'failed', 'Not eligible for Corpus coverage'),
    'found': ('Found in corpus', 'found', 'Confirmed URL presence'),
    'missing': ('Missing from corpus', 'missing', 'Confirmed URL absence'),
    'pending': ('Not checked', 'pending', 'No confirmed Corpus result yet'),
    'checking': ('Checking corpus', 'processing', 'Verification in progress'),
    'unverified': ('Needs review', 'warning', 'Verification did not establish presence or absence'),
    'not_applicable': ('Not applicable', 'pending', 'Website validation is required'),
    'not_tracked': ('Not tracked by corpus', 'pending', 'Corpus does not index this resource type; not a crawler miss'),
}
METHOD_LABELS = {'SEED': 'Entered website', 'STATIC': 'Page links', 'SITEMAP': 'Sitemap',
    'JS_DISCOVERY': 'Rendered page links', 'API_DISCOVERY': 'Observed API', 'FILTER_GET': 'GET filter',
    'PAGINATION': 'Pagination', 'STRUCTURED_DATA': 'Structured data', 'HTTP_LINK': 'HTTP link header',
    'FEED': 'Feed', 'INCREMENTAL_RECHECK': 'Saved recheck', 'JS_SOURCE': 'JavaScript source',
    'METADATA': 'Published metadata', 'PUBLIC_API': 'Public content API',
    'RESOURCESYNC': 'ResourceSync inventory', 'EXTERNAL_INDEX': 'Historical URL index'}


def text_comparison_state(row):
    """Expose existing saved comparisons, never infer completeness from URL presence.

    The historical comparator is token-set similarity, not an exact percentage
    of all page text captured. New API comparisons measure live-word retention.
    """
    percent = None
    if row.get('page_type') != 'HTML':
        label = 'Not applicable · URL only'
    elif not is_inventory_url(row):
        label = 'Website text not validated'
    elif row.get('match_status') == 'NOT_FOUND':
        label = 'No corpus record to compare'
    elif row.get('text_status') == 'API_TEXT_NOT_SUPPORTED':
        label = 'Unavailable from Corpus API'
    elif not is_found(row):
        label = 'Awaiting corpus verification'
    else:
        similarity = row.get('similarity')
        has_text = (row.get('text_length') or 0) > 0 and row.get('corpus_text_length') is not None
        if row.get('text_status') in ASSESSED_TEXT_STATUSES and has_text and type(similarity) in (float, int) and math.isfinite(similarity) and 0 <= similarity <= 1:
            percent = round(similarity * 100, 1)
        label = TEXT_LABELS.get(row.get('text_status'), 'Not compared')
    try:
        evidence = json.loads(row.get('evidence_json') or '{}')
        method = evidence.get('text_comparison', {}).get('method', 'token_set_similarity')
    except (ValueError, TypeError, AttributeError):
        method = 'token_set_similarity'
    return {'text_coverage_percent': percent, 'text_coverage_label': label,
            'text_coverage_method': method if percent is not None else None}


def public_states(row):
    valid = is_inventory_url(row)
    website = 'fetching' if row.get('state') == 'FETCHING' else 'rendering' if row.get('state') == 'RENDERING' else 'collected' if row.get('validity') == 'URL_ONLY' else 'validated' if valid else 'discovered' if not row.get('validity') else 'rendering' if row.get('validity') == 'RENDER_REQUIRED' else 'failed'
    corpus = 'not_applicable' if not valid else 'checking' if row.get('state') == 'CHECKING_CORPUS' else 'not_tracked' if row.get('match_status') == 'NOT_TRACKED' else 'found' if is_found(row) else 'missing' if row.get('match_status') == 'NOT_FOUND' else 'unverified' if row.get('match_status') else 'pending'
    text = 'URL only' if row.get('page_type') != 'HTML' else 'Live text captured' if (row.get('text_length') or 0) > 0 else 'Rendering required' if row.get('validity') == 'RENDER_REQUIRED' else 'Not assessed' if row.get('text_length') is None else 'No readable live text'
    retry_warning = valid and row.get('last_attempt_validity') not in {None, 'VALID_PAGE', 'URL_ONLY'}
    return {'website_state': website, 'website_label': 'Previously validated · latest retry failed' if retry_warning else LABELS[website][0],
            'retry_warning': bool(retry_warning), 'corpus_state': corpus,
            'corpus_label': LABELS[corpus][0], 'status': 'processing' if corpus == 'checking' or website in {'fetching', 'rendering'} else 'failed' if website == 'failed' else corpus,
            'text_label': text, 'eligible': valid, **text_comparison_state(row)}
