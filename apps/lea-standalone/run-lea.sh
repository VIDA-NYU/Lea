#!/usr/bin/env bash
# =============================================================================
# Lea — one-command launcher.
#
# Share THIS SINGLE FILE. Recipients only need Docker Desktop installed; this
# script pulls the prebuilt image from VIDA's GitHub Container Registry and runs the whole app
# (web UI + adapter + bundled Lea prover + Lean + Mathlib) in one container.
#
#   ./run-lea.sh
#
# Then open http://localhost:8001 and add your provider API key in Settings.
# Your settings/key, proof history, and event logs persist in ./lea-config and
# ./lea-data next to this script.
# =============================================================================
set -euo pipefail

# Published image (override with: LEA_IMAGE=you/repo:tag ./run-lea.sh)
IMAGE="${LEA_IMAGE:-ghcr.io/vida-nyu/leaui:main}"
CONTAINER="lea-interface"
PORT="${LEA_PORT:-8001}"

cd "$(dirname "$0")"
DATA_DIR="$(pwd)/lea-data"
CONFIG_DIR="$(pwd)/lea-config"

echo "=== Lea ==="

# 1. Docker must be installed and running.
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Desktop and try again:"
  echo "  https://www.docker.com/products/docker-desktop/"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop isn't running. Start it (whale icon in the menu bar),"
  echo "wait until it says 'running', then run this script again."
  exit 1
fi

mkdir -p "$DATA_DIR" "$CONFIG_DIR"

# 2. Pull the latest image (first run downloads several GB — be patient).
echo "Pulling the Lea image ($IMAGE)…"
echo "(First run downloads several GB of Lean + Mathlib. Later runs are instant.)"
docker pull "$IMAGE"

# 3. Replace any previous container.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# 4. Open the browser once the app is healthy (in the background).
(
  for _ in $(seq 1 900); do
    if curl -fsS "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
      if command -v open >/dev/null 2>&1; then open "http://localhost:${PORT}"
      elif command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:${PORT}"
      fi
      break
    fi
    sleep 2
  done
) &

# 5. Run. Foreground so Ctrl+C stops it cleanly.
echo "Starting Lea on http://localhost:${PORT}  (press Ctrl+C to stop)"
echo "When it opens, add your provider API key in Settings to run proofs."
exec docker run --rm --name "$CONTAINER" \
  --init \
  -p "${PORT}:8001" \
  -p "31245:31245" \
  -v "$DATA_DIR:/app/data" \
  -v "$CONFIG_DIR:/app/config" \
  -v "lea-overleaf-state:/app/apps/overleaf-extension/.overleaf-lean-stub" \
  -v "lea-mathlib-build:/app/prover/workspace/.lake/packages/mathlib/.lake/build" \
  "$IMAGE"
