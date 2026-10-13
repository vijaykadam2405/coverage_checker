"""URL cells link out — but only for schemes that are safe to click.

Every URL on this dashboard came off a CRAWLED PAGE, i.e. from a third-party website. esc() makes
it safe to DISPLAY; it does not make it safe to put in an href. `javascript:` in an anchor runs on
click with this console's session, and this console can stop crawls and delete the corpus.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "src" / "mallory_orchestrator" / "static" / "index.html"
SRC = HTML.read_text(encoding="utf-8")


def test_the_safe_href_helper_exists():
    assert "const safeHref" in SRC and "const urlCell" in SRC


def test_only_http_and_https_are_linkable():
    """The allowlist must be an allowlist, not a blocklist of known-bad schemes."""
    m = re.search(r"const safeHref[\s\S]{0,400}?\};", SRC)
    assert m, "safeHref not found"
    body = m.group(0)
    assert '"http:"' in body and '"https:"' in body
    assert "javascript" not in body.lower(), (
        "blocklisting javascript: invites the next scheme through — allowlist instead")


def test_it_parses_with_the_URL_constructor_not_a_regex():
    """`java\tscript:` and `JaVaScRiPt:` defeat string checks; the URL parser normalises them."""
    m = re.search(r"const safeHref[\s\S]{0,400}?\};", SRC)
    assert "new URL(" in m.group(0)


def test_the_drilldown_table_uses_it():
    """The table the operator actually clicks — the per-site URL list."""
    assert "urls.map(u => `<tr>` + urlCell(u.url)" in SRC


def test_no_url_cell_still_interpolates_a_raw_href():
    """Any remaining `href="${esc(...url...)}"` bypasses the scheme guard."""
    bad = re.findall(r'href="\$\{esc\((?:[a-z]\.)?url[^)]*\)\}"', SRC)
    assert not bad, "unguarded url in an href: %r" % bad


def test_links_are_noopener_and_noreferrer():
    """target=_blank without noopener hands the opened page a window.opener handle back to this
    console; noreferrer keeps the dashboard's URL out of third-party logs."""
    m = re.search(r"const urlCell[\s\S]{0,600}?\};", SRC)
    assert 'rel="noopener noreferrer"' in m.group(0)


def test_the_script_block_still_parses():
    """A syntax error here returns HTTP 200 and a silently dead page — the documented failure."""
    import shutil, subprocess, tempfile, os
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    block = re.findall(r"<script>([\s\S]*?)</script>", SRC)
    assert len(block) == 1
    fd, path = tempfile.mkstemp(suffix=".js"); os.close(fd)
    Path(path).write_text(block[0], encoding="utf-8")
    try:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:400]
    finally:
        os.unlink(path)
