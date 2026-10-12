#!/usr/bin/env bash
# Starts the UI adapter (:8001), which serves the built frontend AND drives the
# Lea prover in-process (architecture D1 — no separate Lea API process).
#
# No API key is needed to start. The container boots keyless; the user adds and
# validates their key in the app's Settings pane, which saves it to
# /app/config/lea.local.toml (mounted as a volume so it persists). The adapter
# loads that key from config and exports it to its own environment per run.
set -euo pipefail

echo "[lea] starting UI on http://localhost:8001"
echo "[lea] open it, then add your API key in Settings (no key needed to start)."
cd /app/adapter
exec .venv/bin/python run_api.py --host 0.0.0.0 --port 8001
