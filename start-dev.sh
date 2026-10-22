#!/usr/bin/env bash
#
# Start the whole Lea dev stack:
#   - standalone UI: FastAPI adapter (:8001) + Vite frontend (:5173)  [npm run dev:ui]
#   - Overleaf companion (:31245)                                     [npm run dev:overleaf]
#
# By default previous session data is KEPT (PLAN-system-hardening 0.3 — wiping
# proofs and history must be opt-in, not the default of the most-typed command).
#
# Pass --fresh (or -f) to first clear ALL previous session data (the adapter
# SQLite DB — sessions/runs/code steps — plus proof files, project entries, and
# companion job logs) via `npm run reset:local`, after confirmation.
#
# --keep-data (-k) is accepted for backward compatibility; it is now the default.
#
# Stop everything with Ctrl+C; press it again to force quit.

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
      echo "  --fresh, -f   confirm, then clear previous session data (proofs, sessions, logs)"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: $(basename "$0") [--fresh|-f]" >&2
      exit 1
      ;;
  esac
done

# Check before --fresh as well as before spawning services. A duplicate launch
# must not clear the data of a stack that is still running.
node scripts/dev-stack-preflight.mjs stack

if [[ "$FRESH" -eq 1 ]]; then
  echo "[start] WARNING: --fresh will delete your local data, including proofs, projects, sessions, and logs."
  while true; do
    printf '[start] Delete this data and continue? [y/n] '
    if ! IFS= read -r confirmation; then
      echo
      echo "[start] Cancelled. Your data has been kept."
      exit 0
    fi
    case "$confirmation" in
      y|Y) break ;;
      n|N)
        echo "[start] Cancelled. Your data has been kept."
        exit 0
        ;;
      *) echo "[start] Please enter y or n." ;;
    esac
  done
fi

LOCAL_CONFIG="apps/lea-standalone/config/lea.local.toml"
LOCAL_CONFIG_EXAMPLE="apps/lea-standalone/config/lea.local.example.toml"

if [[ ! -e "$LOCAL_CONFIG" && ! -L "$LOCAL_CONFIG" ]]; then
  if [[ ! -f "$LOCAL_CONFIG_EXAMPLE" ]]; then
    echo "[start] Cannot create $LOCAL_CONFIG: example config is missing at $LOCAL_CONFIG_EXAMPLE." >&2
    exit 1
  fi

  # The Settings UI may later store provider keys here, so create it owner-only.
  (umask 077; cp "$LOCAL_CONFIG_EXAMPLE" "$LOCAL_CONFIG")
  echo "[start] Created $LOCAL_CONFIG from the example config."
fi

# Reset must happen BEFORE the adapter starts — it deletes the SQLite file, and
# the adapter recreates it (with the current schema) on startup.
if [[ "$FRESH" -eq 1 ]]; then
  echo "[start] --fresh: clearing previous session data..."
  npm run reset:local
else
  echo "[start] Keeping previous session data (pass --fresh to start from a clean slate)."
fi

echo "[start] Starting adapter, frontend, and Overleaf companion."
# One owner launches the actual services and reaps their process groups. Using
# exec also delivers both Ctrl+C presses directly to that owner.
exec node scripts/supervisor.mjs stack
