"""Response-backed JSON continuation, adapted from ZIP parse.links_from_json.

No arbitrary cursor generation. Arithmetic requires a populated result array
AND an explicit continuation flag or published numeric bound.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _integer(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def continuations(payload: dict, url: str) -> list[str]:
    if not isinstance(payload, dict):
        return []
    envelopes = [payload]
    for name in ('meta', 'pagination', 'paging', 'pageInfo', 'response'):
        if isinstance(payload.get(name), dict):
            envelopes.append(payload[name])
    flat = {key.lower().replace('_', ''): value for item in envelopes for key, value in item.items()}
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    results = []
    mapping = {'nextcursor': {'cursor'}, 'nextcursormark': {'cursormark'}, 'nextoffset': {'offset', 'start', 'from'},
               'nextpage': {'page', 'paged', 'p', 'pg'}}
    for field, names in mapping.items():
        value = flat.get(field)
        if value is None or value == '' or isinstance(value, (bool, dict, list)):
            continue
        if field not in {'nextcursor', 'nextcursormark'} and _integer(value) is None:
            continue  # A next URL is handled by the normal published-link extractor.
        for index, (name, current) in enumerate(pairs):
            if name.lower() in names and str(value) != current:
                changed = pairs.copy()
                changed[index] = (name, str(value))
                results.append(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(changed), '')))
    if results:
        return results
    if any(flat.get(name) is True for name in ('endofrecords', 'lastpage', 'islast', 'done')) or any(
            flat.get(name) is False for name in ('hasmore', 'hasnext', 'hasnextpage')):
        return []
    # An unchanged opaque cursor cannot be a new URL even when results exist.
    if any(name.lower() in {'cursor', 'cursormark'} for name, _ in pairs):
        return []
    arrays = [value for item in envelopes for name, value in item.items() if name.lower() in
              {'results', 'items', 'hits', 'data', 'records', 'entries'} and isinstance(value, list)]
    count = max((len(value) for value in arrays), default=0)
    if not count:
        return []
    pages = next((_integer(flat[name]) for name in ('totalpages', 'nbpages', 'pagecount') if _integer(flat.get(name)) is not None), None)
    total = next((_integer(flat[name]) for name in ('total', 'totalresults', 'totalcount', 'nbhits') if _integer(flat.get(name)) is not None), None)
    more = any(flat.get(name) is True for name in ('hasmore', 'hasnext', 'hasnextpage'))
    size = next((_integer(value) for name, value in pairs if name.lower() in {'limit', 'size', 'per_page', 'pagesize', 'page_size'} and _integer(value)), count)
    for index, (name, value) in enumerate(pairs):
        current = _integer(value)
        if current is None:
            continue
        if name.lower() in {'offset', 'start', 'from'}:
            following = current + size
            if total is None and not more or total is not None and following >= total:
                continue
        elif name.lower() in {'page', 'paged', 'p', 'pg'}:
            following = current + 1
            zero_based = 'nbpages' in flat
            if pages is not None and following >= pages + (0 if zero_based else 1):
                continue
            # A total-item count alone does not establish zero/one-based page indexing.
            if pages is None and not more:
                continue
        else:
            continue
        changed = pairs.copy()
        changed[index] = (name, str(following))
        return [urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(changed), ''))]
    return []
