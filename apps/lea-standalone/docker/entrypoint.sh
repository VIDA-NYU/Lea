#!/usr/bin/env bash
# Starts BOTH backend processes for the two front ends and supervises them:
#   - FastAPI adapter (:8001)  — serves the built UI and drives the prover in-process
#   - Overleaf companion (:31245) — backend for the Chrome extension
# One container is the whole backend for both front ends. If either process
# exits, the container comes down so the failure surfaces (and `docker compose`
# stop/restart stays clean).
#
# No API key is needed to start. The container boots keyless; the user adds and
# validates their key in the app's Settings pane, which saves it to
# /app/config/lea.local.toml (mounted as a volume so it persists). The companion
# reads shared settings/keys from the adapter, so there is nothing to configure
# for the Overleaf side either.
set -uo pipefail

if ! /app/prepare-mathlib.sh; then
  echo "[lea] Mathlib could not be prepared; the backends will not start." >&2
  exit 1
fi

ADAPTER_PID=0
COMPANION_PID=0

# Forward termination to both children (idempotent; disarm the trap first so a
# second signal during shutdown can't re-enter).
term() {
  trap - TERM INT
  kill -TERM "$ADAPTER_PID" "$COMPANION_PID" 2>/dev/null || true
}
trap term TERM INT

echo "[lea] starting adapter (UI) on http://localhost:8001"
( cd /app/adapter && exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 ) &
ADAPTER_PID=$!

echo "[lea] starting Overleaf companion on http://localhost:31245"
( cd /app/apps/overleaf-extension && exec node companion/server.mjs ) &
COMPANION_PID=$!

echo "[lea] both backends up. Open http://localhost:8001 and add your API key in Settings."

# Wait for whichever backend exits first, then bring the container down.
wait -n
status=$?
echo "[lea] a backend process exited (status $status) — shutting down the container."
term
wait 2>/dev/null || true
exit "$status"
