"""Check Git publication safety and optionally run the local release checks.

Never stages, commits, pushes, starts scans or queries the production Corpus.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SUFFIXES = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".session",
    ".sqlite",
    ".sqlite-wal",
    ".sqlite-shm",
    ".db",
    ".db-wal",
    ".db-shm",
    ".log",
    ".pyc",
)
GENERATED_PARTS = {
    "node_modules",
    "__pycache__",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
}
GENERATED_ROOTS = {"data", "reports", "output", "tmp", "legacy"}
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"\bcov_[A-Za-z0-9_-]{32,}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
]


def publication_issues(
    relative: str, content: bytes, *, active_key: bytes = b""
) -> list[str]:
    parts = Path(relative).parts
    name = parts[-1].lower()
    issues = []
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        issues.append("local environment file")
    if name.endswith(PRIVATE_SUFFIXES):
        issues.append("local credential, certificate or runtime file")
    if (
        parts[0] in GENERATED_ROOTS
        or GENERATED_PARTS.intersection(parts)
        or parts[:2] == ("frontend", "dist")
    ):
        issues.append("generated/runtime directory")
    if len(content) > 50 * 1024 * 1024:
        issues.append("file exceeds 50 MiB publication safeguard")
    if (
        any(pattern.search(content) for pattern in SECRET_PATTERNS)
        or active_key
        and active_key in content
    ):
        issues.append("possible secret; inspect locally without sharing its contents")
    return issues


def check_repository(root: Path = ROOT) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    # Check tracked and publishable untracked files, not .gitignored local data.
    paths = sorted(set(result.stdout.decode("utf-8").split("\0")) - {""})
    active_key = b""
    if root == ROOT:
        sys.path.insert(0, str(ROOT))
        from crawler_coverage_auditor.corpus.credentials import load_key

        try:
            active_key = load_key().encode()
        except ValueError:
            pass  # A contributor need not have a production key to run checks.
    findings = []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            continue  # Intended deletions remain visible in git status.
        if path.is_symlink():
            findings.append(
                (relative, "symbolic link needs explicit publication review")
            )
            continue
        if path.stat().st_size > 50 * 1024 * 1024:
            findings.append((relative, "file exceeds 50 MiB publication safeguard"))
            continue
        for issue in publication_issues(
            relative, path.read_bytes(), active_key=active_key
        ):
            findings.append((relative, issue))
    for relative, issue in findings:
        print(f"[FAIL] {relative}: {issue}")
    if not findings:
        print(
            f"[PASS] Publication check: {len(paths)} tracked/publishable files; no flagged working-tree secrets or runtime files."
        )
    return not findings


def check_history(root: Path = ROOT, *, all_refs: bool = False) -> bool:
    """Inspect HEAD ancestry (or explicitly all refs), without printing values.

    A normal single-branch push sends HEAD ancestry, not unrelated local backups.
    Use all_refs when auditing every local ref; never use --all or --mirror to push
    branches that have not separately passed publication review.
    """
    pattern = (
        r"cov_[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|"
        r"github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|"
        r"-----BEGIN (RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    )
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "log",
            "--all" if all_refs else "HEAD",
            "--format=COMMIT:%H",
            "--name-only",
            "--no-renames",
            "-G",
            pattern,
            "--",
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    commit = None
    findings = set()
    for line in result.stdout.decode("utf-8").splitlines():
        if line.startswith("COMMIT:"):
            commit = line[7:]
        elif line and commit:
            blob = subprocess.run(
                ["git", "show", f"{commit}:{line}"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if blob.returncode == 0 and any(
                expression.search(blob.stdout) for expression in SECRET_PATTERNS
            ):
                findings.add((commit[:12], line))
    for commit, path in sorted(findings):
        print(
            f"[FAIL] Git history {commit}:{path}: possible credential/private key. Rotate it and review history before publishing."
        )
    if not findings:
        print(
            f"[PASS] Git history ({'all local refs' if all_refs else 'HEAD ancestry'}): no matching common credential/private-key patterns."
        )
    return not findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-only",
        action="store_true",
        help="Only inspect publication safety in the working tree and HEAD ancestry",
    )
    parser.add_argument(
        "--all-history",
        action="store_true",
        help="Also audit unrelated local branches/tags; not needed for a single-branch push",
    )
    args = parser.parse_args()
    tree_ok = check_repository()
    history_ok = check_history(all_refs=args.all_history)
    if not tree_ok or not history_ok:
        return 1
    if args.repository_only:
        return 0
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        print("[FAIL] Node.js/npm is required for frontend verification.")
        return 1
    checks = [
        ("Dependency consistency", [sys.executable, "-m", "pip", "check"]),
        (
            "Python import/name checks",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--select",
                "F,I",
                "crawler_coverage_auditor",
                "scripts",
                "tests",
                "--exclude",
                "vendor",
            ],
        ),
        ("Backend tests", [sys.executable, "-m", "pytest", "-q"]),
        (
            "Frontend formatting, tests and production build",
            [npm, "--prefix", "frontend", "run", "check"],
        ),
        (
            "Git whitespace check",
            ["git", "-c", "core.safecrlf=false", "diff", "--check"],
        ),
    ]
    for label, command in checks:
        print(f"Checking {label}...", flush=True)
        if subprocess.run(command, cwd=ROOT).returncode:
            print(f"[FAIL] {label}")
            return 1
    print("[PASS] Local pre-push checks completed. No commit or push was performed.")
    print(
        "Pattern checks are not an exhaustive secret audit or proof of production API availability / website completeness."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
