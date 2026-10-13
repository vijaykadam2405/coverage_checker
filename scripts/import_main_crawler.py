"""Mechanically import the user's complete source ZIP, never execute its contents.

Existing files are never overwritten with different bytes. Git history, secrets,
runtime databases/sessions and generated results are not application features.
The manifest accounts for EVERY archive file, including exclusions/redactions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SECRET = re.compile(rb'(?:cov_[A-Za-z0-9]{25,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
RUNTIME_NAMES = {'results.jsonl', 'trap_results.jsonl', 'checklist_status.jsonl', 'edgecase_report.xlsx',
                 'persite.json', 'corpuspages.json', 'pages_attr.json', 'man_attr.json', 'samples.json'}
TEXT_SUFFIXES = {'.py', '.js', '.mjs', '.html', '.css', '.md', '.json', '.jsonl', '.yml', '.yaml', '.txt', '.toml', '.csv', '.sh', '.ps1', '.sql', '.conf'}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def exclusion(path: PurePosixPath) -> str | None:
    if any(part in {'.git', '__pycache__', '.venv', 'node_modules'} for part in path.parts):
        return 'Repository metadata or installed/generated dependency cache'
    if any(part in {'data', 'sessions', 'state', 'logs'} for part in path.parts):
        return 'Runtime state/session directory; do not import production data'
    if path.name.startswith('.env'):
        return 'Environment values deliberately excluded; configure local optional services yourself'
    if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.log', '.session', '.pyc', '.pyo'} or path.name.endswith(('-wal', '-shm')):
        return 'Runtime database/log/session or bytecode'
    if path.name in RUNTIME_NAMES:
        return 'Generated production/benchmark results, not source functionality'
    return None


def import_source(source: Path, destination: Path) -> dict:
    destination = destination.resolve()
    if destination.name != 'main_crawler' or destination.parent.name != 'vendor':
        raise ValueError('Use an explicit vendor/main_crawler destination, not a workspace/home root')
    plan, records = [], []
    with ZipFile(source) as archive:
        files = [entry for entry in archive.infolist() if not entry.is_dir()]
        if sum(entry.file_size for entry in files) > 256 * 1024 * 1024:
            raise ValueError('Archive exceeds import-size safety limit')
        seen = set()
        for entry in files:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename or not path.parts or path.parts[0] != 'Crawler':
                raise ValueError('Unsafe or unexpected archive path')
            if PureWindowsPath(entry.filename).is_reserved() or any(part.endswith(('.', ' ')) for part in path.parts):
                raise ValueError('Unsafe Windows archive filename')
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Archive symlinks are not imported')
            relative = PurePosixPath(*path.parts[1:])
            if str(relative).casefold() in seen:
                raise ValueError('Duplicate archive destination')
            seen.add(str(relative).casefold())
            reason = exclusion(relative)
            if reason:
                records.append({'source': entry.filename, 'path': str(relative), 'status': 'excluded', 'reason': reason})
                continue
            if entry.file_size > 32 * 1024 * 1024:
                raise ValueError('Individual source artifact exceeds safety limit')
            original = archive.read(entry)
            copied = original
            if relative.suffix.lower() in TEXT_SUFFIXES:
                copied = SOURCE_SECRET.sub(b'REDACTED_SOURCE_CREDENTIAL', copied)
            elif SOURCE_SECRET.search(copied):
                records.append({'source': entry.filename, 'path': str(relative), 'status': 'excluded', 'reason': 'Sensitive token in binary artifact'})
                continue
            target = (destination / str(relative)).resolve()
            if not target.is_relative_to(destination):
                raise ValueError('Import target escapes vendor directory')
            if target.exists() and (not target.is_file() or target.read_bytes() != copied):
                raise ValueError(f'Refusing to overwrite changed imported file: {relative}')
            plan.append((target, copied))
            records.append({'source': entry.filename, 'path': str(relative), 'status': 'redacted' if copied != original else 'copied',
                            'source_sha256': digest(original), 'copied_sha256': digest(copied),
                            'text_sha256': digest(copied.replace(b'\r', b'')) if relative.suffix.lower() in TEXT_SUFFIXES or relative.name.startswith('Dockerfile') else None,
                            'bytes': len(copied)})
    summary = {status: sum(record['status'] == status for record in records) for status in ['copied', 'redacted', 'excluded']}
    manifest = {'schema': 1, 'source_archive': source.name, 'archive_sha256': digest(source.read_bytes()),
                'policy': 'Complete source preserved as optional; CrawlScope remains HTML-text-only, URL-only files, serial and read-only Corpus',
                'summary': summary, 'python_sources': sum(record['path'].endswith('.py') and record['status'] != 'excluded' for record in records),
                'files': records}
    encoded = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    manifest_path = destination / 'IMPORT_MANIFEST.json'
    upgrade_manifest = False
    if manifest_path.exists() and manifest_path.read_bytes() != encoded:
        old = json.loads(manifest_path.read_bytes())
        comparable = json.loads(encoded)
        for record in comparable['files']:
            record.pop('text_sha256', None)
        if old != comparable:
            raise ValueError('Existing import manifest differs; choose a separate reviewed revision rather than overwriting')
        upgrade_manifest = True  # Same bytes/revision; add line-ending-tolerant verification metadata only.
    for target, content in plan:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)  # Mechanical source import, not handwritten file edits.
    if not manifest_path.exists() or upgrade_manifest:
        manifest_path.write_bytes(encoded)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, default=ROOT / 'vendor/main_crawler')
    args = parser.parse_args()
    manifest = import_source(args.source.resolve(), args.destination)
    print(json.dumps({'archive_sha256': manifest['archive_sha256'], 'python_sources': manifest['python_sources'], **manifest['summary']}))


if __name__ == '__main__':
    main()
