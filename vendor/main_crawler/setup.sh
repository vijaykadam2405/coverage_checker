#!/usr/bin/env bash
# Bring the Layer-1 stack up and prove it is actually working.
#
#   ./setup.sh              build + start + health check
#   ./setup.sh --check      health check only, touches nothing
#   ./setup.sh --test       health check + the code's own self-checks
#   ./setup.sh --tunnel     also start the SSH tunnel to the data centre
#
# Bash rather than PowerShell so the same file runs on the Linux server and in Git Bash on Windows.
#
# Two things this refuses to do:
#   * rebuild while a crawl is running — the frontier is in memory, so a restart discards every
#     discovered-but-unfetched URL and the next run has to re-walk the whole discovery surface
#   * overwrite an existing .env
#
# A check that cannot run reports UNKNOWN, never PASS. "We could not measure this" and "this is
# fine" must never look the same, or the summary at the end is worthless.
set -uo pipefail
cd "$(dirname "$0")"

MODE=setup; TUNNEL=0; RUN_TESTS=0
for a in "$@"; do case "$a" in
  --check)  MODE=check ;;
  --test)   RUN_TESTS=1 ;;
  --tunnel) TUNNEL=1 ;;
  -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown option: $a (try --help)"; exit 2 ;;
esac; done

C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_0=$'\033[0m'
[ -t 1 ] || { C_OK=; C_BAD=; C_WARN=; C_DIM=; C_0=; }

PASS=0; FAIL=0; UNKNOWN=0
declare -a NOTES
ok()   { PASS=$((PASS+1));    printf '  %sPASS%s  %-34s %s\n' "$C_OK"   "$C_0" "$1" "${2-}"; }
bad()  { FAIL=$((FAIL+1));    printf '  %sFAIL%s  %-34s %s\n' "$C_BAD"  "$C_0" "$1" "${2-}"; NOTES+=("$1: ${2-}"); }
unk()  { UNKNOWN=$((UNKNOWN+1)); printf '  %s????%s  %-34s %s\n' "$C_WARN" "$C_0" "$1" "${2-}"; }
head_() { printf '\n%s── %s %s\n' "$C_DIM" "$1" "$C_0"; }

COMPOSE=(docker compose -f docker-compose.yml)
[ "$TUNNEL" = 1 ] && COMPOSE+=(-f docker-compose.tunnel.yml)

# ── 0 · prerequisites ────────────────────────────────────────────────────────
head_ "prerequisites"
command -v docker >/dev/null 2>&1 || { bad "docker" "not installed"; echo; echo "install Docker first"; exit 1; }
ok "docker" "$(docker --version | cut -d, -f1)"
docker compose version >/dev/null 2>&1 || { bad "docker compose" "v2 plugin missing"; exit 1; }
ok "docker compose" "$(docker compose version --short 2>/dev/null)"
docker info >/dev/null 2>&1 || { bad "docker daemon" "not running — start Docker Desktop"; exit 1; }
ok "docker daemon" "running"

# Disk. The vhdx/volumes grow with the corpus and a full disk corrupts Postgres rather than
# politely failing, so this is a hard stop, not a warning.
FREE=$(df -Pk . 2>/dev/null | awk 'NR==2{print int($4/1048576)}')
if [ -n "${FREE:-}" ] && [ "$FREE" -lt 5 ]; then
  bad "disk space" "${FREE}GB free — Postgres can corrupt below this; free space first"
elif [ -n "${FREE:-}" ] && [ "$FREE" -lt 20 ]; then
  unk "disk space" "${FREE}GB free — tight for a large crawl"
else
  ok "disk space" "${FREE:-?}GB free"
fi

# ── 1 · configuration ────────────────────────────────────────────────────────
head_ "configuration"
if [ ! -f .env ]; then
  if [ "$MODE" = check ]; then
    bad ".env" "missing (run without --check to create it from .env.example)"
  else
    cp .env.example .env
    ok ".env" "created from .env.example — edit it before a production run"
    NOTES+=(".env was just created from the example; secrets are placeholders")
  fi
else
  ok ".env" "present ($(grep -cE '^[A-Z]' .env) settings)"
fi
# The tunnel authenticates with a key, not the password in .env.deploy.
if [ "$TUNNEL" = 1 ]; then
  [ -f secrets/mallory_deploy ] && ok "ssh key" "secrets/mallory_deploy" \
    || bad "ssh key" "secrets/mallory_deploy missing — the tunnel cannot authenticate"
fi

# ── 2 · never rebuild over a live crawl ──────────────────────────────────────
if [ "$MODE" = setup ]; then
  head_ "safety"
  RUNNING=$(curl -fsS --max-time 5 http://localhost:8099/v1/batch/status 2>/dev/null \
            | grep -o '"running":[a-z]*' | cut -d: -f2)
  if [ "${RUNNING:-}" = "true" ]; then
    bad "active crawl" "a batch is RUNNING"
    cat <<'WARN'

  Rebuilding now would discard the in-memory frontier: every URL discovered but not yet
  fetched is lost, and the next run has to re-walk the discovery surface to find them again.

  Stop it first, then re-run:
      curl -X POST http://localhost:8099/v1/crawl/stop
WARN
    exit 1
  fi
  ok "active crawl" "none — safe to build"

  head_ "build + start"
  "${COMPOSE[@]}" build || { bad "build" "see output above"; exit 1; }
  ok "build" "images built"
  "${COMPOSE[@]}" up -d || { bad "start" "see output above"; exit 1; }
  ok "start" "containers up"
fi

# ── 3 · wait for the stack ───────────────────────────────────────────────────
head_ "services"
wait_for() {   # name url seconds
  local n=$1 u=$2 t=${3:-90} i=0
  while [ $i -lt "$t" ]; do
    curl -fsS --max-time 4 "$u" >/dev/null 2>&1 && { ok "$n" "$u"; return 0; }
    sleep 2; i=$((i+2))
  done
  bad "$n" "no answer from $u after ${t}s"
  return 1
}
wait_for "crawler-api  :8099" http://localhost:8099/health 120
wait_for "ingest       :9090" http://localhost:9090/health 120
wait_for "orchestrator :8090" http://localhost:8090/health 120

for c in postgres minio; do
  cid=$("${COMPOSE[@]}" ps -q "$c" 2>/dev/null | head -1)
  if [ -z "$cid" ]; then unk "$c" "container not found"; continue; fi
  st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null)
  case "$st" in healthy|running) ok "$c" "$st" ;; *) bad "$c" "${st:-unknown}" ;; esac
done

# ── 4 · does it actually work ────────────────────────────────────────────────
head_ "function"
jnum() { grep -o "\"$2\":[0-9]*" <<<"$1" | head -1 | cut -d: -f2; }   # first numeric field
# printf "%'d" needs a locale that is not guaranteed on a bare server image, so group by hand.
comma() { printf '%s' "${1:-0}" | sed -E ':a;s/^([0-9]+)([0-9]{3})/\1,\2/;ta'; }

S=$(curl -fsS --max-time 30 http://localhost:9090/api/live/summary 2>/dev/null)
if [ -z "$S" ]; then
  bad "document store" "ingest did not answer"
else
  DOCS=$(jnum "$S" documents)
  # 0 documents is legitimate on a fresh install and must not read as a failure.
  [ -n "$DOCS" ] && ok "document store" "$(comma "$DOCS") documents" \
                 || bad "document store" "no count in the response (Postgres unreachable?)"
  OBJ=$(jnum "$S" objects)
  [ -n "$OBJ" ] && ok "blob store" "$(comma "$OBJ") objects" \
               || unk "blob store" "MinIO did not report — check credentials"
fi

T=$(curl -fsS --max-time 45 http://localhost:8099/v1/tiers 2>/dev/null)
if [ -z "$T" ]; then
  unk "fetch tiers C1/C2/C3" "probe endpoint did not answer"
else
  for t in c1 c2 c3; do
    up=$(grep -o "\"$t\":{[^}]*" <<<"$T" | grep -o '"up":[a-z]*' | cut -d: -f2)
    case "$up" in
      true)  ok "tier ${t^^}" "reachable" ;;
      false) unk "tier ${t^^}" "down — the ladder still works, this tier is skipped" ;;
      *)     unk "tier ${t^^}" "not reported" ;;
    esac
  done
fi

O=$(curl -fsS --max-time 30 http://localhost:8090/api/data/summary 2>/dev/null)
grep -q '"reachable":true' <<<"$O" && ok "dashboard -> ingest" "orchestrator sees the store" \
  || bad "dashboard -> ingest" "orchestrator cannot read the store"

# ── 5 · data centre (optional; unreachable is normal) ────────────────────────
head_ "data centre"
P=$(curl -fsS --max-time 30 http://localhost:9090/api/live/push/status 2>/dev/null)
if [ -z "$P" ]; then
  unk "data centre" "status endpoint did not answer"
elif grep -q '"centre_url":null' <<<"$P"; then
  unk "data centre" "DATA_CENTRE_DSN not set — the push button stays disabled"
else
  PEND=$(jnum "$P" pending)
  if grep -q '"reachable":true' <<<"$P"; then
    ok "data centre" "reachable · $(comma "${PEND:-0}") document(s) not yet pushed"
  else
    D=$(grep -o '"detail":"[^"]*' <<<"$P" | cut -d'"' -f4)
    unk "data centre" "unreachable (${D:-no detail}) — data stays local until it returns"
  fi
fi

# Is this machine visible to the fleet dashboard? "my heartbeat is not arriving" and "the dashboard
# is not showing me" look identical from the far end, so ask the machine itself — the answer here
# is what the centre would see. An unnamed node is the one failure worth stopping for: the default
# is the hostname, and two boxes cloned from one image would collapse onto a single dashboard row.
F=$(curl -fsS --max-time 30 http://localhost:8099/v1/fleet/self 2>/dev/null)
if [ -z "$F" ]; then
  unk "fleet report" "crawler-api did not answer"
elif grep -q '"enabled":false' <<<"$F"; then
  unk "fleet report" "FLEET_HEARTBEAT=0 — this machine reports nothing"
else
  NODE=$(grep -o '"node_id":"[^"]*' <<<"$F" | head -1 | cut -d'"' -f4)
  if grep -q '"ok":true' <<<"$F"; then
    ok "fleet report" "reporting as '${NODE}'"
  elif ! grep -q '"ok":' <<<"$F"; then
    unk "fleet report" "'${NODE}' has not reported yet"
  else
    E=$(grep -o '"error":"[^"]*' <<<"$F" | head -1 | cut -d'"' -f4)
    unk "fleet report" "'${NODE}' cannot reach the centre (${E:-no detail}) — crawling is unaffected"
  fi
  # Read .env, not the shell: setup.sh runs on the host and compose is what loads .env, so testing
  # $MALLORY_NODE_ID here would warn on every correctly-configured machine.
  grep -qE '^\s*MALLORY_NODE_ID\s*=\s*\S' .env 2>/dev/null || unk "machine name" \
    "MALLORY_NODE_ID unset — using the hostname '${NODE}'; set it in .env before cloning this box"
fi

# ── 6 · the code's own self-checks ───────────────────────────────────────────
if [ "$RUN_TESTS" = 1 ]; then
  head_ "self-checks"
  # MSYS_NO_PATHCONV: Git Bash rewrites anything that looks like a unix path before handing it to a
  # native .exe, so "/app/tests/x.py" reached the container as "C:/Program Files/Git/app/tests/x.py".
  # Ignored by every non-Windows shell, so the same line works on the server.
  export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
  run_in() {  # label service command...
    local label=$1 svc=$2; shift 2
    if out=$("${COMPOSE[@]}" exec -T "$svc" "$@" 2>&1); then
      ok "$label" "$(tail -1 <<<"$out")"
    else
      bad "$label" "$(tail -1 <<<"$out")"
    fi
  }
  run_in "importer parser"  orchestrator python -c "import sys;sys.path.insert(0,'/app/src');from mallory_orchestrator import importer;importer.demo()"
  run_in "dashboard api"    orchestrator python /app/test_api_smoke.py
  run_in "ingest loss path" crawler-api  python /app/tests/test_ingest_loss.py
  run_in "c3 status guard"  crawler-api  python /app/tests/test_c3_status.py
  run_in "push to centre"   crawler-api  python /app/tests/test_push_centre.py
fi

# ── summary ──────────────────────────────────────────────────────────────────
printf '\n%s────────────────────────────────────────────────────────%s\n' "$C_DIM" "$C_0"
printf '  %s%d passed%s   %s%d unknown%s   %s%d failed%s\n' \
  "$C_OK" "$PASS" "$C_0" "$C_WARN" "$UNKNOWN" "$C_0" "$C_BAD" "$FAIL" "$C_0"
if [ ${#NOTES[@]} -gt 0 ]; then
  printf '\n  needs attention:\n'
  for n in "${NOTES[@]}"; do printf '    · %s\n' "$n"; done
fi
if [ "$FAIL" -eq 0 ]; then
  cat <<EOF

  dashboards:
    http://localhost:8090   control panel — sources, jobs, schedule, logs
    http://localhost:9090   stored data — what actually landed
    http://localhost:8099   crawler internals — live batch
EOF
fi
# UNKNOWN does not fail the run: a down C2 or an unreachable data centre are normal operating
# states, not broken installs. Only a real failure is an error.
exit $(( FAIL > 0 ? 1 : 0 ))
