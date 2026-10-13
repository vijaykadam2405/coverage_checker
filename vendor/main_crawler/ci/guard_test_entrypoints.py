#!/usr/bin/env python3
"""Every test file must actually be RUN by pytest.

    python ci/guard_test_entrypoints.py crawler/tests orchestrator/tests

This repo writes many suites as `t_*` functions driven by a `main()`, with a thin
`def test_<name>_suite(): main()` so pytest has something to collect. Forget that one wrapper and
pytest collects NOTHING from the file — it does not error, it reports green, and the suite silently
stops running.

That is not hypothetical. Sixteen crawler test files once ran ZERO assertions while reporting
success, and the trap has already returned: as of 2026-08-19,
orchestrator/tests/test_logs_outcome.py holds five `t_*` functions and no entry point, so none of
them have executed since the file was written.

Also flags a file with no test functions at all, which is the same failure wearing a different hat.
"""
from __future__ import annotations

import ast
import pathlib
import sys


def check(dirs: list[str]) -> int:
    bad: list[str] = []
    empty: list[str] = []
    files = 0
    for d in dirs:
        for f in sorted(pathlib.Path(d).rglob("test_*.py")):
            files += 1
            try:
                tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as e:
                bad.append("%s — does not parse: %s" % (f, e))
                continue
            fns = [n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            collectable = [n for n in fns if n.startswith("test_")]
            t_style = [n for n in fns if n.startswith("t_")]
            if t_style and not collectable:
                bad.append("%s — %d t_* function(s) and no test_* entry point; pytest runs none of them"
                           % (f, len(t_style)))
            elif not collectable and not t_style:
                empty.append(str(f))

    print("checked %d test file(s)" % files)
    for line in empty:
        print("  note: %s defines no test functions" % line)
    if bad:
        print("\nFAIL — these files are collected as green while running nothing:")
        for line in bad:
            print("  " + line)
        print("\nFix: add `def test_<name>_suite(): main()` at the bottom of the file.")
        return 1
    print("every test file has a pytest entry point")
    return 0


if __name__ == "__main__":
    raise SystemExit(check(sys.argv[1:] or ["crawler/tests", "orchestrator/tests"]))
