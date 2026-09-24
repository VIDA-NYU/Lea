#!/usr/bin/env bash
#
# Start the whole Lea dev stack:
#   - standalone UI: FastAPI adapter (:8001) + Vite frontend (:5173)  [npm run dev:ui]
#   - Overleaf companion (:31245)                                     [npm run dev:overleaf]
#
# If :8001 is taken on your machine, set LEA_ADAPTER_PORT to move the adapter (the
# Vite /api proxy follows it). The Overleaf companion needs to be pointed at the
# same place via LEA_API_BASE_URL in the root .env, e.g.
#   LEA_ADAPTER_PORT=8011 ./start-dev.sh   with LEA_API_BASE_URL=http://127.0.0.1:8011
#
# By default previous session data is KEPT (PLAN-system-hardening 0.3 — wiping
# proofs and history must be opt-in, not the default of the most-typed command).
#
# Pass --fresh (or -f) to first clear ALL previous session data (the adapter
# SQLite DB — sessions/runs/code steps — plus proof files, project entries, and
# companion job logs) via `npm run reset:local`.
#
# --keep-data (-k) is accepted for backward compatibility; it is now the default.
#
# Stop everything with Ctrl+C.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

FRESH=0
for arg in "$@"; do
  case "$arg" in
    -f|--fresh) FRESH=1 ;;
    -k|--keep-data)
      echo "[start] Note: --keep-data is now the default; the flag is no longer needed."
      ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--fresh|-f]"
      echo "  (default)     keep previous session data and start everything"
      echo "  --fresh, -f   clear previous session data first (proofs, sessions, logs)"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $(basename "$0") [--fresh|-f]" >&2
      exit 1
      ;;
  esac
done

# Reset must happen BEFORE the adapter starts — it deletes the SQLite file, and
# the adapter recreates it (with the current schema) on startup.
if [[ "$FRESH" -eq 1 ]]; then
  echo "[start] --fresh: clearing previous session data..."
  npm run reset:local
else
  echo "[start] Keeping previous session data (pass --fresh to start from a clean slate)."
fi

pids=()
cleaned_up=0
cleanup() {
  [[ "$cleaned_up" -eq 1 ]] && return
  cleaned_up=1
  echo
  echo "[start] Shutting down..."
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM

echo "[start] Starting UI (adapter :${LEA_ADAPTER_PORT:-8001} + frontend :5173)..."
npm run dev:ui &
pids+=($!)

echo "[start] Starting Overleaf companion (:31245)..."
npm run dev:overleaf &
pids+=($!)

echo "[start] All processes launched. Press Ctrl+C to stop."

# If any child exits, tear the rest down too. (Portable poll — avoids `wait -n`,
# which needs bash 4+, while macOS still ships bash 3.2.)
while :; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      cleanup
      exit 0
    fi
  done
  sleep 1
done
