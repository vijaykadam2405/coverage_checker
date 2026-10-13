"""Detect backend source changes without inspecting credentials or runtime data."""
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from time import monotonic


@lru_cache(maxsize=4)
def _revision(files):
    digest = sha256()
    for relative, absolute, size, modified in files:
        digest.update(relative.encode('utf-8') + b'\0')
        digest.update(Path(absolute).read_bytes())
    return digest.hexdigest()


def source_revision(root):
    root = Path(root).resolve()
    paths = sorted((root / 'crawler_coverage_auditor').rglob('*.py'))
    paths += [path for path in (root / 'dashboard.py', root / 'audit.py') if path.is_file()]
    files = []
    for path in paths:
        stat = path.stat()
        files.append((path.relative_to(root).as_posix(), str(path), stat.st_size, stat.st_mtime_ns))
    return _revision(tuple(files))


class BackendRevision:
    def __init__(self, root, *, clock=monotonic):
        self.root = root
        self.loaded = source_revision(root)
        self.clock = clock
        self._checked_at = float('-inf')
        self._changed = False

    def update_available(self):
        now = self.clock()
        if now - self._checked_at >= 5:
            try:
                self._changed = source_revision(self.root) != self.loaded
            except OSError:
                # A file replacement in progress must not break scan monitoring.
                self._changed = True
            self._checked_at = now
        return self._changed
