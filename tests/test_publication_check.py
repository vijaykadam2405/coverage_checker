import subprocess

import pytest

from scripts.prepush_check import check_history, check_repository, publication_issues


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "folder/.env.production",
        "data/coverage.sqlite",
        "reports/run_3/coverage.html",
        "frontend/dist/index.html",
        "frontend/node_modules/package/index.js",
        "local.pem",
        "private.key",
        "browser.session",
    ],
)
def test_local_and_generated_files_cannot_be_published(path):
    assert publication_issues(path, b"ordinary content")


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "README.md",
        "crawler_coverage_auditor/auditor.py",
        "frontend/src/App.jsx",
        ".github/workflows/ci.yml",
        "vendor/main_crawler/IMPORT_MANIFEST.json",
    ],
)
def test_normal_source_files_are_allowed(path):
    assert not publication_issues(path, b"ordinary content")


def test_secret_detection_does_not_need_or_return_secret_contents():
    secret = b"cov_" + b"a" * 40
    assert publication_issues("settings.py", secret)
    assert publication_issues(
        "settings.py", b"custom-active-key", active_key=b"custom-active-key"
    )
    assert publication_issues("key.txt", b"-----BEGIN " + b"PRIVATE KEY-----")


def test_tracked_ignored_file_is_still_detected(tmp_path, capsys):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text("private content that must not be echoed")
    subprocess.run(["git", "add", "-f", ".env"], cwd=tmp_path, check=True)
    assert not check_repository(tmp_path)
    output = capsys.readouterr().out
    assert ".env" in output and "private content" not in output


def test_removed_credential_still_blocks_publication_from_history(tmp_path, capsys):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    fixture = tmp_path / ".env.example"
    secret = "cov_" + "a" * 40
    fixture.write_text("API_KEY=" + secret)
    for message in ("first", "removed"):
        subprocess.run(["git", "add", ".env.example"], cwd=tmp_path, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release@example.test",
                "commit",
                "-q",
                "-m",
                message,
            ],
            cwd=tmp_path,
            check=True,
        )
        fixture.write_text("API_KEY=")
    assert check_repository(tmp_path)
    assert not check_history(tmp_path)
    output = capsys.readouterr().out
    assert "Git history" in output and secret not in output


def test_unrelated_backup_history_is_not_part_of_single_branch_push(tmp_path, capsys):
    def git(*args):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release@example.test",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    git("init", "-q", "-b", "private-backup")
    fixture = tmp_path / ".env.example"
    secret = "cov_" + "b" * 40
    fixture.write_text("API_KEY=" + secret)
    git("add", ".env.example")
    git("commit", "-q", "-m", "private backup")
    git("checkout", "--orphan", "release")
    fixture.write_text("API_KEY=")
    git("add", ".env.example")
    git("commit", "-q", "-m", "clean release")

    assert check_repository(tmp_path)
    assert check_history(tmp_path)
    assert not check_history(tmp_path, all_refs=True)
    assert secret not in capsys.readouterr().out
