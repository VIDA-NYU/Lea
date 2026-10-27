#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
browser_waiter=""
finish() {
  result=$?
  trap - EXIT
  if [[ -n "$browser_waiter" ]]; then kill "$browser_waiter" 2>/dev/null || true; fi
  if (( result != 0 && result != 130 )); then
    echo "Lea could not start. The error above identifies the failed download or service."
    echo "Keep this directory and its data; do not reset or delete volumes."
  fi
  echo "Lea has stopped. Press Enter to close."
  read -r _ || true
  exit "$result"
}
trap finish EXIT
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo "Install and start Docker Desktop: https://www.docker.com/products/docker-desktop/"
  exit 1
fi
docker compose version
echo "Downloading this release's pinned images. Keep at least 20 GB free in Docker storage."
docker compose -f compose.yaml pull
(
  for ((attempt=0; attempt<600; attempt++)); do
    if curl -fsS http://localhost:8001/api/ready >/dev/null 2>&1 \
      && curl -fsS http://localhost:31245/health >/dev/null 2>&1; then
      if [[ "$(uname -s)" == Darwin ]]; then open http://localhost:8001
      elif command -v xdg-open >/dev/null; then xdg-open http://localhost:8001
      else echo "Lea is ready: http://localhost:8001"; fi
      exit 0
    fi
    sleep 2
  done
  echo "Lea has not become ready. Check the service logs above."
) &
browser_waiter=$!
docker compose -f compose.yaml up
