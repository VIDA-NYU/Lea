# Install Lea with Docker

The VIDA repository publishes a self-contained image with the standalone UI,
shared adapter, in-process prover, Overleaf companion, Lean, and Mathlib.

## Requirements

- Docker Desktop (or Docker Engine with Compose)
- One API key from OpenAI, Anthropic, or Google Gemini
- Google Chrome if you want the Overleaf extension

No Node, Python, `uv`, Lean, or Mathlib installation is required.

## Download and start

```sh
git clone https://github.com/VIDA-NYU/LeaUIOverleafEcosystem.git
cd LeaUIOverleafEcosystem/apps/lea-standalone
docker compose pull
docker compose up
```

The image is `ghcr.io/vida-nyu/leaui:main` and supports
`linux/amd64` and `linux/arm64`. The first download is several gigabytes
because Lean and Mathlib are included.

Open <http://localhost:8001>, then use **Settings** to choose a model and paste
the corresponding provider key. The app starts without a key.

On macOS, you can double-click `start-lea.command` instead of running the two
Compose commands manually.

## Use the Overleaf extension

The same container runs the Overleaf companion on
<http://127.0.0.1:31245>. Load the Chrome extension once:

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select `apps/overleaf-extension/extension` from this checkout.
5. Leave the companion URL at `http://127.0.0.1:31245`.

Keep the container running while you use Lea in Overleaf.

## Update

The `:main` tag follows the VIDA repository's current `main` branch. Export
important projects, then update with:

```sh
docker compose pull
docker compose up
```

## Local data

Compose persists:

- `./data`: sessions, timeline metadata, and adapter logs
- `./config`: settings and provider keys
- `./overleaf-state`: companion job state
- `lea-mathlib-build`: the verified Mathlib build cache

The proof workspace is not currently mounted on the host. Export important
projects before `docker compose down` or replacing the image.

## Build from source

Developers can build the same Dockerfile from the VIDA checkout:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

This uses the monorepo root as the build context and produces the local image
`leaui:local`.

## Stop and troubleshoot

Stop with `Ctrl+C`. If startup fails:

```sh
docker compose pull
docker compose logs
```

Confirm that ports `8001` and `31245` are free and that Docker Desktop is
running. The container health check requires both the adapter and Overleaf
companion to respond.
