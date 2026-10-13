"""Use reviewed pure helpers from the complete imported main-crawler source.

Never import the fleet, ingestion, signup or solver entrypoints in a normal scan.
Use a private package name so the production `crawler` package cannot shadow ours.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / 'vendor/main_crawler'
PACKAGE = '_crawlscope_main_crawler'
PURE_MODULES = frozenset({'parse', 'captcha', 'stealth', 'errors', 'canonicalize'})
LOCK = threading.RLock()


def enabled() -> bool:
    return os.getenv('CRAWLSCOPE_MAIN_CRAWLER_HELPERS', '1') == '1'


@lru_cache(maxsize=1)
def manifest():
    path = VENDOR / 'IMPORT_MANIFEST.json'
    if not path.is_file():
        raise RuntimeError('Main crawler source is missing; run scripts/import_main_crawler.py first')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 1:
        raise RuntimeError('Unsupported main crawler source manifest')
    return data


def _verified_path(relative):
    record = next((item for item in manifest()['files'] if item['path'] == relative and item['status'] in {'copied', 'redacted'}), None)
    path = (VENDOR / relative).resolve()
    if not path.is_relative_to(VENDOR.resolve()) or record is None or not path.is_file():
        raise RuntimeError('Main crawler helper is missing from the reviewed source import')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != record['copied_sha256'] and (
            not record.get('text_sha256') or hashlib.sha256(content.replace(b'\r', b'')).hexdigest() != record['text_sha256']):
        raise RuntimeError('Main crawler helper changed after import; verify the source revision')
    return path


@lru_cache(maxsize=len(PURE_MODULES))
def load_helper(name):
    if name not in PURE_MODULES:
        raise ValueError('Only reviewed pure helpers may be loaded in the coverage process')
    with LOCK:
        if PACKAGE not in sys.modules:
            path = _verified_path('crawler/crawler/__init__.py')
            spec = importlib.util.spec_from_file_location(PACKAGE, path, submodule_search_locations=[str(path.parent)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[PACKAGE] = package
            try:
                spec.loader.exec_module(package)
            except BaseException:
                sys.modules.pop(PACKAGE, None)
                raise
        full_name = PACKAGE + '.' + name
        path = _verified_path(f'crawler/crawler/{name}.py')
        spec = importlib.util.spec_from_file_location(full_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(full_name, None)
            raise
        return module


def html_links(html, base_url):
    if not enabled():
        return []
    with LOCK:
        parser = load_helper('parse')
        links = parser.extract_links_with_text(html, base_url)
        links.extend((url, 'Main crawler pagination relation') for url in parser.extract_rel_links(html, base_url))
        return links


def http_links(header, base_url):
    if not enabled():
        return []
    with LOCK:
        return load_helper('parse').links_from_link_header(header, base_url)


def challenge(status, headers, html):
    if not enabled() or status in {401, 404, 410, 429, 451, 511}:
        return None
    detection = load_helper('captcha').detect_http(status, headers, html)
    # A widget/vendor presence alone is not proof of withheld website content.
    return detection if detection.found and detection.blocking else None


def browser_profile():
    active = enabled() and os.getenv('CRAWLSCOPE_MAIN_CRAWLER_STEALTH', '0') == '1'
    if not active:
        return {'enabled': False, 'args': [], 'ignore_default_args': [], 'init_script': None}
    helper = load_helper('stealth')
    if not helper.stealth_enabled():
        return {'enabled': False, 'args': [], 'ignore_default_args': [], 'init_script': None}
    return {'enabled': True, 'args': helper.stealth_launch_args(),
            'ignore_default_args': helper.stealth_ignore_default_args(), 'init_script': helper.STEALTH_INIT_JS}


def status():
    data = manifest()
    identity_enabled = browser_profile()['enabled']
    return {'archive_sha256': data['archive_sha256'], 'python_source_files': data['python_sources'],
            'import_counts': data['summary'], 'helpers_enabled': enabled(),
            'browser_identity_enabled': identity_enabled,
            'features': [
                {'feature': 'Original HTML / relation / HTTP Link discovery', 'state': 'active' if enabled() else 'disabled'},
                {'feature': 'Original challenge/vendor classification', 'state': 'active' if enabled() else 'disabled'},
                {'feature': 'Original browser identity helpers', 'state': 'optional; enabled' if identity_enabled else 'optional; disabled'},
                *[{'feature': name, 'state': 'source retained; not activated in coverage scans'} for name in
                  ['CamoFox / host solver / paid CAPTCHA providers', 'Login, account registration and session recipes',
                   'Proxy routing and spend budgets', 'Full asynchronous engine / fleet / governors',
                   'PDF/document extraction / attachments', 'Postgres / MinIO / ingestion',
                   'Orchestrator schedules / sharding / categories', 'Archive replay / specialised harvesters']]],
            'coverage_rules': {'html_text_only': True, 'files_url_only': True, 'sequential': True, 'corpus_readonly': True}}


def verify_source(*, check_syntax=False):
    import ast
    data = manifest()
    missing, changed, syntax_errors, checked, line_endings = [], [], [], 0, 0
    for record in data['files']:
        if record['status'] == 'excluded':
            continue
        path = (VENDOR / record['path']).resolve()
        if not path.is_relative_to(VENDOR.resolve()) or not path.is_file():
            missing.append(record['path'])
            continue
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record['copied_sha256']:
            if record.get('text_sha256') and hashlib.sha256(content.replace(b'\r', b'')).hexdigest() == record['text_sha256']:
                line_endings += 1
            else:
                changed.append(record['path'])
                continue
        checked += 1
        if check_syntax and path.suffix == '.py':
            try:
                ast.parse(content.decode('utf-8-sig').replace('\r', ''), filename=record['path'])
            except (SyntaxError, UnicodeError):
                syntax_errors.append(record['path'])
    return {'ok': not (missing or changed or syntax_errors), 'checked_files': checked,
            'line_ending_only_changes': line_endings, 'missing': missing, 'changed': changed,
            'syntax_errors': syntax_errors, 'excluded_files': data['summary']['excluded']}
