import ast
from pathlib import Path


def test_static_scheduler_never_starts_parallel_url_workers() -> None:
    source = Path("crawler_coverage_auditor/auditor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_crawl_static")
    assert not any(isinstance(node, ast.Call) and getattr(node.func, "attr", "") in {"gather", "create_task"} for node in ast.walk(method))
    calls = [getattr(node.value.func, "attr", "") for node in ast.walk(method)
             if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)]
    assert sorted(calls) == sorted(["_drain_pagination", "_process_url", "_crawl_browser", "_verify_all", "_drain_pagination", "step", "step"])
