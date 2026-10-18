#!/usr/bin/env bash
# One role per container. Compose runs the migration job before API and worker.
set -euo pipefail
cd /app/adapter
case "${1:-api}" in
  migrate) exec .venv/bin/python bootstrap.py ;;
  api) exec .venv/bin/python run_api.py --host 0.0.0.0 --port 8001 ;;
  worker)
    /app/prepare-mathlib.sh
    exec .venv/bin/python run_worker.py ;;
  companion) cd /app/apps/overleaf-extension; exec node companion/server.mjs ;;
  *) echo "Usage: entrypoint.sh migrate|api|worker|companion" >&2; exit 2 ;;
esac
