"""No dashboard query may touch main_text or html to answer a question a _len column already holds.

THIS DEFECT HAS NOW APPEARED THREE TIMES, in three different queries, and each time it presented as
"the database is down" rather than as a slow query:

  1. documents_current's sort key used length(main_text) — the view detoasted ~8 GB on every read.
  2. by_source() used `d.html <> ''` to count pages-with-html. Measured 2026-08-12: over 6 minutes
     per run, 3.4 cores pegged at 0 MB/s of disk (pure decompression — the heap was already cached),
     28 queries stacked behind the dashboard's pollers, and the Watchfloor's whole corpus panel
     reading "corpus unreachable" over a perfectly healthy Postgres.
  3. _TOTALS_SQL summed length(main_text), the export sorted on it, the "text" dtype filter compared
     main_text to '', and live.py counted empty pages with length(main_text)=0 — four more, found
     only because #2 sent someone looking.

main_text and html are TOASTed. Any expression that reads their VALUE — length(), <> '', IS NOT
NULL used as a proxy for non-empty — decompresses every row it touches. `text_len` and `html_len`
exist for precisely this, are maintained by trigger, and are fully backfilled (verified on the live
corpus: 0 NULLs of 1,168,313 rows).

The permitted form is `coalesce(text_len, length(main_text), 0)`. COALESCE short-circuits, so the
length() never evaluates while the column is populated; it is correctness insurance for a row
inserted before its trigger fires, not a cost. A BARE length(main_text) is the bug.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "ingest_api"
FILES = ["pgstore.py", "live.py", "app.py"]

# `coalesce(<len col>, length(<toasted col>), ...)` — the short-circuiting form, always allowed.
_GUARDED = re.compile(r"coalesce\(\s*(?:d\.)?(?:text_len|html_len)\s*,\s*length\(", re.I)

# The forms that decompress. Each is a real regression that reached production.
_BANNED = [
    (re.compile(r"\blength\(\s*(?:d\.)?main_text\s*\)", re.I), "length(main_text)"),
    (re.compile(r"\blength\(\s*(?:d\.)?html\s*\)", re.I), "length(html)"),
    (re.compile(r"\boctet_length\(\s*(?:d\.)?(?:main_text|html)\s*\)", re.I), "octet_length(...)"),
    (re.compile(r"\b(?:d\.)?main_text\s*(?:<>|=|!=)\s*''", re.I), "main_text <> ''"),
    (re.compile(r"\b(?:d\.)?html\s*(?:<>|=|!=)\s*''", re.I), "html <> ''"),
]


def _sql_lines(path: Path):
    """Every line that could reach the database, with prose removed.

    Comments AND docstrings. Stripping only `#` and `--` was not enough: the module documents this
    very defect in triple-quoted docstrings ("USES text_len, NEVER length(main_text)"), so the guard
    flagged the warning against the bug as if it were the bug. Prose that names the banned form is
    exactly what a file explaining the banned form contains.

    Docstrings are detected by tokenize rather than by regex — a `\"\"\"` inside a SQL string
    literal must not start a "docstring" and swallow the real queries after it.
    """
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    skip: set[int] = set()
    try:
        prev_end = (0, 0)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING and tok.line.strip().startswith(('"""', "'''", 'r"""')):
                # a bare string expression on its own line = a docstring, not a query
                if prev_end[0] != tok.start[0]:
                    skip.update(range(tok.start[0], tok.end[0] + 1))
            if tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
                prev_end = tok.end
    except (tokenize.TokenError, IndentationError):
        skip = set()                       # unparseable: scan everything rather than nothing

    out = []
    for n, raw in enumerate(src.splitlines(), 1):
        if n in skip:
            continue
        line = re.sub(r"--.*", "", re.sub(r"#.*", "", raw))
        if line.strip():
            out.append((n, line))
    return out


def _offenders():
    bad = []
    for f in FILES:
        p = ROOT / f
        if not p.exists():
            continue
        for n, line in _sql_lines(p):
            for rx, label in _BANNED:
                for m in rx.finditer(line):
                    # allowed when it sits inside the short-circuiting coalesce
                    window = line[max(0, m.start() - 60):m.end()]
                    if _GUARDED.search(window):
                        continue
                    bad.append("%s:%d  %s  ->  %s" % (f, n, label, line.strip()[:90]))
    return bad


def test_no_query_reads_a_toasted_column_to_get_its_size():
    bad = _offenders()
    assert not bad, (
        "these expressions decompress main_text/html to answer something text_len/html_len already "
        "hold. On a 72 GB corpus that is minutes per query and it presents as an outage, not as "
        "slowness. Use coalesce(text_len, length(main_text), 0) / coalesce(html_len, 0):\n  "
        + "\n  ".join(bad))


def test_the_scanner_catches_a_planted_regression(tmp_path, monkeypatch):
    """A green result must mean 'no offenders', not 'the scan is broken'. Every banned form is
    planted and must be reported — an earlier version of this guard missed `<> ''` entirely, which
    is the exact form that caused the outage."""
    planted = tmp_path / "pgstore.py"
    planted.write_text(
        "Q1 = 'SELECT length(main_text) FROM documents'\n"
        "Q2 = 'SELECT length(html) FROM documents'\n"
        "Q3 = \"SELECT count(*) FILTER (WHERE main_text <> '') FROM documents\"\n"
        "Q4 = \"SELECT count(*) FILTER (WHERE d.html <> '') FROM documents_current d\"\n"
        "Q5 = 'SELECT octet_length(main_text) FROM documents'\n",
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "FILES", ["pgstore.py"])
    bad = _offenders()
    assert len(bad) == 5, "the scan should flag all five planted forms; got %d:\n%s" % (
        len(bad), "\n".join(bad))


def test_docstring_skipping_does_not_hide_real_queries(tmp_path, monkeypatch):
    """The risk the docstring fix introduces, tested directly.

    Every real query in this codebase lives in a TRIPLE-QUOTED string — `_SQL = \"\"\"SELECT ...` or
    `query(\"\"\"SELECT ...`. If the docstring detector were even slightly too eager it would skip
    those and the guard would pass on a corpus-detoasting query, which is worse than not having the
    guard at all: it would report safety it never checked."""
    f = tmp_path / "pgstore.py"
    f.write_text(
        '"""Module docstring that mentions length(main_text) as a warning."""\n'
        'import x\n'
        '\n'
        '_TOTALS = """SELECT coalesce(sum(length(main_text)), 0)\n'
        '              FROM documents_current"""\n'
        '\n'
        'def f():\n'
        '    """Docstring saying NEVER use html <> \'\' here."""\n'
        '    return query("""SELECT count(*) FILTER (WHERE d.html <> \'\')\n'
        '                    FROM documents_current d""")\n',
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "FILES", ["pgstore.py"])
    bad = _offenders()
    assert len(bad) == 2, (
        "must flag the two REAL queries and neither docstring; got %d:\n%s" % (len(bad), "\n".join(bad)))
    assert any("length(main_text)" in b for b in bad) and any("html <> ''" in b for b in bad), bad


def test_the_guarded_coalesce_form_is_allowed(tmp_path, monkeypatch):
    """The permitted form must NOT be flagged, or the guard becomes noise someone deletes."""
    ok = tmp_path / "pgstore.py"
    ok.write_text(
        "Q1 = 'ORDER BY coalesce(text_len, length(main_text), 0) DESC'\n"
        "Q2 = 'SELECT coalesce(sum(coalesce(text_len, length(main_text), 0)), 0)'\n"
        "Q3 = 'WHERE coalesce(html_len, 0) > 0'\n",
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "FILES", ["pgstore.py"])
    assert _offenders() == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
