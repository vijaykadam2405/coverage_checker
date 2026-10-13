#!/usr/bin/env bash
# Daily health-check wrapper for the Mallory crawler, meant to be run once a day by
# cron or a systemd timer ON the data-centre box.
#
#   ./healthcheck/run_daily.sh
#
# It runs the suite as a one-shot overlay container on the live stack, tees the report
# to a timestamped log, keeps a `-latest` symlink, and exits non-zero if anything
# failed — so the scheduler (or you) can alert on it. Read-only against production.
set -uo pipefail

# Repo root = the dir this script's parent lives in (…/mallory-crawler). Override with REPO_DIR.
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="${HC_LOG_DIR:-$REPO_DIR/healthcheck/logs}"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/healthcheck-$TS.log"

cd "$REPO_DIR" || { echo "cannot cd to $REPO_DIR"; exit 2; }

# HC_LOG_DIR is what the compose file mounts to /logs inside the container.
export HC_LOG_DIR="$LOG_DIR"
export HC_JSON_OUT="/logs/healthcheck-$TS.json"

docker compose -f docker-compose.yml -f docker-compose.healthcheck.yml \
  run --rm healthcheck 2>&1 | tee "$LOG"
rc="${PIPESTATUS[0]}"

ln -sf "healthcheck-$TS.log" "$LOG_DIR/healthcheck-latest.log"
[ -f "$LOG_DIR/healthcheck-$TS.json" ] && ln -sf "healthcheck-$TS.json" "$LOG_DIR/healthcheck-latest.json"

# Prune logs older than 30 days so this never fills the disk.
find "$LOG_DIR" -name 'healthcheck-*.log' -mtime +30 -delete 2>/dev/null || true
find "$LOG_DIR" -name 'healthcheck-*.json' -mtime +30 -delete 2>/dev/null || true

if [ "$rc" -ne 0 ]; then
  echo "HEALTHCHECK FAILED (exit $rc) — see $LOG"
fi
exit "$rc"
