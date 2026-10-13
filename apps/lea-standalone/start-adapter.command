#!/usr/bin/env bash
# Double-click this file in Finder to start the Lea adapter daemon. (macOS)
#
# This runs ONLY the FastAPI adapter (the backend) on http://127.0.0.1:8001 —
# headless, with no browser tab and no Vite dev server. It is the shared local
# host that both the Lea UI and the Overleaf companion talk to. Leave it running
# in the background; the Overleaf extension formalizes against it whether or not
# the UI tab is open. Close this window (or Ctrl+C) to stop it.
set -euo pipefail

cd "$(dirname "$0")"

echo "=== Lea adapter daemon (:8001) ==="

if [ ! -x "adapter/.venv/bin/python" ]; then
  echo "Adapter virtualenv is missing. Run 'npm run setup:api' first."
  echo "Press Enter to close."; read -r _; exit 1
fi

# Refuse to start a second copy on the same port.
if curl -fsS http://127.0.0.1:8001/api/health >/dev/null 2>&1; then
  echo "An adapter is already running on http://127.0.0.1:8001."
  echo "Press Enter to close."; read -r _; exit 0
fi

echo "Starting adapter on http://127.0.0.1:8001 ... (Ctrl+C to stop)"
exec node scripts/dev.mjs --adapter-only
