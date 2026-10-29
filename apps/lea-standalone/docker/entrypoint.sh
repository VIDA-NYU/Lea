#!/usr/bin/env bash
# One role per container. Compose runs the migration job before API and worker.
set -euo pipefail
cd /app/adapter

# Only the workspace worker owns all export sources. Keep its wrapper alive
# across internal worker exec/restarts, then export after its final shutdown.
run_worker() {
  local child=0 stopped=0 status=0
  forward_stop() {
    stopped=1
    if (( child > 0 )); then kill -TERM "$child" 2>/dev/null || true; fi
  }
  finish_worker() {
    local result=$?
    trap - EXIT
    trap '' TERM INT
    if (( child > 0 )); then
      kill -TERM "$child" 2>/dev/null || true
      wait "$child" 2>/dev/null || true
    fi
    echo "[lea] Saving current data without credentials (historical backups excluded)..."
    if ! .venv/bin/python /app/scripts/shutdown_snapshot.py --root /app --reason docker-worker; then
      echo "[lea] Shutdown snapshot failed." >&2
      if (( result == 0 )); then result=1; fi
    fi
    exit "$result"
  }
  trap finish_worker EXIT
  trap forward_stop TERM INT
  /app/prepare-mathlib.sh &
  child=$!
  if (( stopped )); then forward_stop; fi
  if wait "$child"; then status=0; else status=$?; fi
  if (( stopped )); then exit 0; fi
  if (( status != 0 )); then exit "$status"; fi
  .venv/bin/python run_worker.py &
  child=$!
  if (( stopped )); then forward_stop; fi
  if wait "$child"; then status=0; else status=$?; fi
  if (( stopped )); then status=0; fi
  exit "$status"
}

case "${1:-api}" in
  migrate) exec .venv/bin/python bootstrap.py ;;
  api) exec .venv/bin/python run_api.py --host 0.0.0.0 --port 8001 ;;
  worker) run_worker ;;
  companion) cd /app/apps/overleaf-extension; exec node companion/server.mjs ;;
  *) echo "Usage: entrypoint.sh migrate|api|worker|companion" >&2; exit 2 ;;
esac
