# Install Lea with Docker

The VIDA repository publishes a self-contained image with the standalone UI,
shared adapter, in-process prover, Overleaf companion, Lean, and Mathlib.

## Requirements

- Docker Desktop (or Docker Engine with Compose)
- At least 20 GB free in Docker's storage
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
`linux/amd64` and `linux/arm64`. The first download is about 3.7 GB and the
extracted image occupies about 10.7 GB because Lean and Mathlib are included.
Docker needs additional working space while extracting it, so keep at least
20 GB free.

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

The `:main` tag follows the VIDA repository's current `main` branch. Update with:

```sh
docker compose pull
docker compose up
```

## Local data

Compose persists:

- `./data`: sessions, timeline metadata, and adapter logs
- `./config`: settings and provider keys
- `./proofs`: proof and project git repositories
- `./projects`: project registry metadata
- `./overleaf-state`: companion job state
- `lea-mathlib-build`: the verified Mathlib build cache

The host directories survive `docker compose down` and container/image
replacement. The bundled Lake workspace remains in the image so a software
update cannot leave stale Lean or Mathlib metadata on the host.

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
running. If a pull reports `no space left on device`, increase Docker Desktop's
disk usage limit or remove unused images in Docker Desktop. The container health
check requires both the adapter and Overleaf companion to respond.
