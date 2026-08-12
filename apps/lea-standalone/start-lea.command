#!/usr/bin/env bash
# Double-click this file in Finder to start Lea. (macOS)
# It checks Docker, starts the app, and opens the browser once it's ready.
# No key file to edit — you add your API key inside the app, in Settings.
set -euo pipefail

cd "$(dirname "$0")"

echo "=== Lea ==="

# 1. Docker must be installed and running.
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and try again."
  echo "Press Enter to close."; read -r _; exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running. Start Docker Desktop (whale icon in the menu bar), wait until it says 'running', then try again."
  echo "Press Enter to close."; read -r _; exit 1
fi

# 2. Open the browser once the app is healthy (runs in the background).
(
  for _ in $(seq 1 600); do
    if curl -fsS http://localhost:8001/api/health >/dev/null 2>&1; then
      open "http://localhost:8001"
      break
    fi
    sleep 2
  done
) &

# 3. Pull the prebuilt image from GitHub's container registry (GHCR) and start it.
#    (First run downloads 3.7 GB; keep 20 GB free during extraction.)
echo "Starting Lea (first run downloads the image and can take a while)..."
echo "Keep at least 20 GB free in Docker Desktop during the first download."
echo "When the app opens, add your API key in Settings to run proofs."
docker compose pull
docker compose up

echo "Lea has stopped. Press Enter to close."; read -r _
