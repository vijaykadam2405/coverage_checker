"""Pytest config — force the deterministic offline (fixtures-only) mode and a
throwaway data dir so tests never touch the network or the real crawl history.
Must run before any crawler module imports (config reads these at import)."""
import os
import tempfile

os.environ["CRAWLER_PREFER_FIXTURES"] = "1"
os.environ["CRAWLER_ALLOW_NETWORK"] = "0"
os.environ.setdefault("CRAWLER_DATA_DIR", tempfile.mkdtemp(prefix="crawler_test_"))
