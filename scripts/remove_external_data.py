"""Remove out-of-scope inventory URLs only, preserving sources and checkpoints."""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler_coverage_auditor.validation.normalize import same_scope
from scripts.remove_eot_data import IDENTITIES, refresh_saved_reports


def remove_external(database: Path, backup: Path, reports: Path | None = None):
    database = database.resolve(strict=True)
    backup = backup.resolve()
    if backup == database.parent or backup == database:
        raise ValueError("Use a new, dedicated backup directory")
    with sqlite3.connect(database, timeout=30) as db:
        db.row_factory = sqlite3.Row
        if db.execute("SELECT 1 FROM audit_runs WHERE status='RUNNING'").fetchone():
            raise ValueError("Pause active scans before removing external URLs")
        backup.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(backup / "coverage.sqlite") as recovery:
            db.backup(recovery)
        settings = database.parent / "capture_settings.json"
        if settings.is_file():
            shutil.copy2(settings, backup / settings.name)
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT 1 FROM audit_runs WHERE status='RUNNING'").fetchone():
                raise ValueError("A scan started; no external URLs were removed")
            removed, per_run = [], {}
            for run in db.execute(
                "SELECT id,target_url,config_json FROM audit_runs"
            ).fetchall():
                config = json.loads(run["config_json"])
                target = (urlsplit(run["target_url"]).hostname or "").lower()
                hosts = set(config.get("seed_hosts") or []) | {target}
                if target.startswith("www."):
                    hosts.add(target.removeprefix("www."))
                for row in db.execute(
                    "SELECT url,normalized_url FROM urls WHERE audit_run_id=?",
                    (run["id"],),
                ):
                    if any(
                        not same_scope(
                            url, hosts, config.get("include_subdomains", False)
                        )
                        for url in (row["url"], row["normalized_url"])
                    ):
                        removed.extend(
                            (run["id"], url)
                            for url in {row["url"], row["normalized_url"]}
                        )
                        per_run[run["id"]] = per_run.get(run["id"], 0) + 1
                config["external_resources"] = False
                db.execute(
                    "UPDATE audit_runs SET config_json=? WHERE id=?",
                    (json.dumps(config), run["id"]),
                )
            if reports:
                for run_id in per_run:
                    source = reports / f"run_{run_id}"
                    if source.is_dir():
                        shutil.copytree(source, backup / "reports" / source.name)
            db.execute(
                "CREATE TEMP TABLE removed_urls(run_id INTEGER,url TEXT,PRIMARY KEY(run_id,url))"
            )
            db.executemany("INSERT INTO removed_urls VALUES(?,?)", removed)
            before = [
                tuple(row)
                for row in db.execute("""SELECT * FROM urls u WHERE NOT EXISTS
                (SELECT 1 FROM removed_urls x WHERE x.run_id=u.audit_run_id AND x.url=u.normalized_url) ORDER BY id""")
            ]
            existing = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            deleted = {}
            for table, fields in IDENTITIES.items():
                if table not in existing:
                    continue
                predicate = " OR ".join(
                    f'x.url="{table}"."{field}"' for field in fields
                )
                deleted[table] = db.execute(f'''DELETE FROM "{table}" WHERE EXISTS
                    (SELECT 1 FROM removed_urls x WHERE x.run_id="{table}".audit_run_id AND ({predicate}))''').rowcount
            # Preserve the run checkpoint and all in-scope URL stages.
            for field in ("current_url", "last_completed_url"):
                db.execute(f'''UPDATE scan_checkpoints SET "{field}"=NULL WHERE EXISTS
                    (SELECT 1 FROM removed_urls x WHERE x.run_id=scan_checkpoints.audit_run_id AND x.url=scan_checkpoints."{field}")''')
            after = [tuple(row) for row in db.execute("SELECT * FROM urls ORDER BY id")]
            if before != after or db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError(
                    "Integrity check failed; external URL removal rolled back"
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
    if settings.is_file():
        value = json.loads(settings.read_text(encoding="utf-8"))
        value["external_resources"] = False
        temporary = settings.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(settings)
    return {
        "removed_urls": sum(per_run.values()),
        "per_run": per_run,
        "affected_runs": list(per_run),
        "deleted_records": deleted,
        "backup": str(backup),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, default=ROOT / "data" / "coverage.sqlite"
    )
    parser.add_argument("--reports", type=Path, default=ROOT / "reports")
    parser.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    backup = (
        args.database.parent
        / "backups"
        / ("before-remove-external-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    )
    result = remove_external(args.database, backup, args.reports)
    refresh_saved_reports(args.database, args.reports, result["affected_runs"])
    print(json.dumps(result))
