#!/usr/bin/env bash
# Supervise both backends. Adapter restart re-execs its child at the same PID;
# adapter shutdown or companion exit terminates the whole container.
set -uo pipefail

ADAPTER_PID=0
COMPANION_PID=0
PREPARE_PID=0
STOP_REQUESTED=0

terminate_children() {
  local pid
  for pid in "$PREPARE_PID" "$ADAPTER_PID" "$COMPANION_PID"; do
    # PID 0 means "not started"; signalling it would kill the process group.
    if (( pid > 0 )); then kill -TERM "$pid" 2>/dev/null || true; fi
  done
}

stop() {
  STOP_REQUESTED=1
  terminate_children
}
trap stop TERM INT

if (( STOP_REQUESTED )); then exit 0; fi
/app/prepare-mathlib.sh &
PREPARE_PID=$!
if (( STOP_REQUESTED )); then terminate_children; fi
wait "$PREPARE_PID"
status=$?
if (( STOP_REQUESTED )); then
  terminate_children
  wait "$PREPARE_PID" 2>/dev/null || true
  exit 0
fi
PREPARE_PID=0
if (( status != 0 )); then
  echo "[lea] Mathlib preparation failed; backends will not start." >&2
  exit "$status"
fi

echo "[lea] starting adapter on :8001"
( cd /app/adapter && exec .venv/bin/python run_api.py --host 0.0.0.0 --port 8001 ) &
ADAPTER_PID=$!
if (( ! STOP_REQUESTED )); then
  echo "[lea] starting Overleaf companion on :31245"
  ( cd /app/apps/overleaf-extension && exec node companion/server.mjs ) &
  COMPANION_PID=$!
fi

if (( STOP_REQUESTED )); then
  status=0
else
  wait -n "$ADAPTER_PID" "$COMPANION_PID"
  status=$?
  if (( STOP_REQUESTED )); then status=0; fi
fi
trap - TERM INT
terminate_children
wait 2>/dev/null || true
exit "$status"
