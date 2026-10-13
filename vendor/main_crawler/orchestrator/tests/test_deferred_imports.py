"""Every `from . import X` written INSIDE a function must actually resolve.

A deferred import survives every import-time check and then raises the first time someone presses
the button that reaches it. That is not hypothetical: `api.py` was ported into the production tree
without `energy_gate.py`, the stack imported cleanly, the whole suite passed, and the Job generator
answered 500 on its first real use with 802 sources waiting to be crawled.

The deferred imports are deliberate — circular dependencies, optional features — so the fix is not
to remove them. It is to check they resolve, which nothing else here does.

Runnable directly:  python tests/test_deferred_imports.py
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_every_deferred_import_in_api_resolves():
    """A `from . import X` INSIDE a function survives every import-time check and then raises the
    first time someone presses the button.

    That is not hypothetical: api.py was copied into the production tree without energy_gate.py, the
    stack imported cleanly, every test passed, and the Job generator answered 500 on its first real
    use with 802 sources waiting. Deferred imports are used deliberately here (circular deps,
    optional features), so the fix is not to remove them — it is to check they resolve.
    """
    import importlib
    # Found rather than assumed: this test is run from the repo (tests/ beside src/) AND copied into
    # the container, where the same file sits at /app with the package under /app/src. A hardcoded
    # ../src path works in one of those and raises FileNotFoundError in the other, which reads as a
    # broken test rather than the missing module it is supposed to be reporting.
    api_src = next((c for c in
                    [Path(__file__).resolve().parents[i] / "src" / "mallory_orchestrator" / "api.py"
                     for i in (1, 0)]
                    + [Path(p) / "mallory_orchestrator" / "api.py" for p in sys.path]
                    if c.exists()), None)
    assert api_src is not None, "could not find api.py from " + str(Path(__file__).resolve())
    text = api_src.read_text(encoding="utf-8")
    # Indented `from . import a, b` — module-level ones are already proved by importing api at all.
    names: set[str] = set()
    for m in re.finditer(r"^\s+from \.\s+import\s+([A-Za-z0-9_, ]+)", text, re.M):
        names |= {n.strip() for n in m.group(1).split(",") if n.strip()}
    missing, skipped = [], []
    for n in sorted(names):
        try:
            importlib.import_module(f"mallory_orchestrator.{n}")
        except ModuleNotFoundError as e:
            # A missing THIRD-PARTY package is this machine lacking sqlalchemy, not a partial port.
            # Only a sibling module that is absent is the bug this test exists for, and that is
            # exactly what it looks like: the name that failed is our own.
            bad = (e.name or "")
            if bad.startswith("mallory_orchestrator") or bad == n:
                missing.append(f"{n} — {e}")
            else:
                skipped.append(f"{n} (needs {bad})")
        except Exception as e:  # noqa: BLE001
            missing.append(f"{n} — {type(e).__name__}: {e}")
    if skipped:
        print(f"  -- not importable here, dependency absent: {', '.join(skipped)}")
    assert not missing, ("api.py defers these imports and they do not resolve, so the endpoint that "
                         f"uses each one will 500 on first use: {missing}")


def main() -> None:
    test_every_deferred_import_in_api_resolves()
    print("  ok  test_every_deferred_import_in_api_resolves")
    print("ALL DEFERRED-IMPORT TESTS PASSED")


if __name__ == "__main__":
    main()
