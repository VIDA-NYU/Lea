# syntax=docker/dockerfile:1

# =============================================================================
# Lea Interface — separate api, worker and companion targets. Only the worker
# contains the workspace, Lean and Mathlib. The API publishes the React UI and
# accesses SQLite; the companion is a stateless transport. Base images and Lean
# toolchain resolve per-arch, so it builds natively on amd64 and arm64 alike.
#
# The build context is the MONOREPO ROOT (not apps/lea-standalone) — the
# companion, shared model catalog, and scripts/ live outside this app and must
# be in the context to be COPYd in. Build/run only via compose or CI, which set
# the context correctly:
#   docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
# Users don't build this at all — docker compose pulls the published image.
#
# Publication is an explicit workflow dispatch: .github/workflows/publish-image.yml.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: build the React frontend into static assets (dist/)
# -----------------------------------------------------------------------------
# Browser assets are architecture-independent. Keep their native build tools on
# the builder's platform; emulated Go/esbuild can crash on cross-platform builds.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS web
WORKDIR /web

# npm workspaces are locked at the monorepo root.  The older app-local lockfile
# is not authoritative and can drift from package.json, so install from the
# root lock while copying only the workspace manifests needed by `npm ci`.
COPY package.json package-lock.json ./
COPY apps/lea-standalone/package.json ./apps/lea-standalone/package.json
COPY apps/overleaf-extension/package.json ./apps/overleaf-extension/package.json
RUN npm ci --workspace apps/lea-standalone

WORKDIR /web/apps/lea-standalone

# Preserve the monorepo-relative layout used by frontend imports while copying
# only the files Vite actually reads (keeps this layer small + cacheable).
COPY apps/lea-standalone/index.html apps/lea-standalone/vite.config.ts apps/lea-standalone/postcss.config.mjs ./
COPY apps/lea-standalone/src ./src
COPY apps/lea-standalone/scripts/lean-assets.mjs ./scripts/lean-assets.mjs
COPY packages/lea-blueprint /web/packages/lea-blueprint
COPY packages/lea-workspace-client /web/packages/lea-workspace-client
RUN npm run build   # -> /web/apps/lea-standalone/dist

# -----------------------------------------------------------------------------
# Target-platform Node, kept separate from the native frontend build stage.
# The companion's executable must match the runtime architecture.
FROM node:22-bookworm-slim AS companion-node

# -----------------------------------------------------------------------------
# Stage 2: runtime — Python 3.13 + uv (base), plus Lean toolchain and Mathlib
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/VIDA-NYU/Lea" \
      org.opencontainers.image.description="Lea theorem-proving UI, adapter, Overleaf companion, Lean, and Mathlib"

# System packages: git (lake fetches Mathlib), curl/ca-certificates (elan +
# health checks), libgmp10 (Lean runtime dependency).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates libgmp10 \
 && rm -rf /var/lib/apt/lists/*

# Install the Lean toolchain manager (elan) and pin the project's Lean version.
ENV ELAN_HOME=/root/.elan
ENV PATH="/root/.elan/bin:${PATH}"
RUN curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o /tmp/elan-init.sh \
 && sh /tmp/elan-init.sh -y --default-toolchain leanprover/lean4:v4.29.0 \
 && rm /tmp/elan-init.sh \
 && lean --version

WORKDIR /app

# --- Vendored Lea prover (driven in-process by the adapter) -----------------
# Copy the prover first so the expensive Mathlib bake is its own cache layer.
COPY apps/lea-standalone/prover ./prover

# Bake Mathlib AND build SafeVerify in one layer so they share a SINGLE Mathlib.
#
# Both the workspace and SafeVerify pin the identical Mathlib commit (and the
# same transitive deps), so SafeVerify symlinks the workspace's already-baked
# `.lake/packages` instead of downloading a second multi-GB copy. The image then
# carries exactly one Mathlib. The dependency metadata is intentionally retained
# so a persisted runtime cache can be validated and repaired after image updates.
#
# `lake exe cache get` fetches Mathlib's prebuilt oleans.  `lake build Mathlib`
# then fills any cache gaps, and `--no-build` is a hard completeness gate.  We
# do NOT run a bare `lake build`: the workspace's @[default_target] `lean_lib
# Lea` is rooted at proofs/, which is empty until the agent writes there at
# runtime. `safe_verify`, by contrast, has its own exe target and builds here.
WORKDIR /app/prover
RUN cd workspace \
 && (lake exe cache get || lake exe cache get \
     || echo "[build] Mathlib cache download incomplete; compiling missing targets.") \
 && lake build Mathlib \
 && lake build --no-build Mathlib \
 && echo "[build] complete Mathlib cache verified" \
 && cd /app/prover/third_party/SafeVerify \
 && rm -rf .lake/packages && mkdir -p .lake \
 && ln -s /app/prover/workspace/.lake/packages .lake/packages \
 && lake build safe_verify \
 && test -x .lake/build/bin/safe_verify \
 && echo "[build] SafeVerify built against the shared Mathlib" \
 && rm -rf /root/.cache/mathlib \
 && echo "[build] retained dependency metadata for runtime cache repair"

# --- UI adapter (FastAPI) ---------------------------------------------------
# `uv sync` builds the vendored prover (lea-prover) as an editable path dep and
# installs dependencies into /app/adapter/.venv. API and worker are separate
# processes; only the private worker imports the prover library.
WORKDIR /app/adapter
COPY apps/lea-standalone/adapter ./
RUN uv sync --frozen --no-dev
ARG LEA_BUILD_SOURCE=development
RUN LEA_BUILD_SOURCE="$LEA_BUILD_SOURCE" .venv/bin/python -c 'import json,os; from pathlib import Path; Path("app/_release.json").write_text(json.dumps({"source":os.environ["LEA_BUILD_SOURCE"]}))'
LABEL org.opencontainers.image.revision=$LEA_BUILD_SOURCE

# --- Overleaf companion (:31245) -------------------------------------------
# The companion has ZERO third-party npm deps (pure Node built-ins + local
# .mjs), so it needs only the target-platform `node` binary — lifted from the
# companion-node stage, same Node 22 and Debian bookworm base as this runtime. Its source is
# laid out at the monorepo-relative paths its imports expect (../../../scripts,
# ../../../packages, ../shared). The adapter-owned private worker targets
# /app/prover via LEA_ROOT; the public transport needs only the adapter URL.
#
# The `extension/` dir is included because it is NOT a clean layering boundary:
# shared/{theoremParser,leanPaneManifest}.mjs import ../extension/targetParserCore.mjs
# at runtime, so the companion crashes without it. Copying the whole (tiny) dir
# is robust against shared/ picking up another extension module later.
COPY --from=companion-node /usr/local/bin/node /usr/local/bin/node
COPY apps/overleaf-extension/companion  /app/apps/overleaf-extension/companion
COPY apps/overleaf-extension/shared     /app/apps/overleaf-extension/shared
COPY apps/overleaf-extension/extension  /app/apps/overleaf-extension/extension
COPY apps/lea-standalone/src/app/lib/leanCheckRuntime.mjs /app/apps/lea-standalone/src/app/lib/leanCheckRuntime.mjs
COPY scripts                            /app/scripts
COPY packages/lea-model-catalog         /app/packages/lea-model-catalog
COPY packages/lea-workspace-client      /app/packages/lea-workspace-client
# Retain the existing volume as a legacy import source. Operational jobs,
# preferences and bounded diagnostics now belong to SQLite in the data volume.
RUN mkdir -p /app/apps/overleaf-extension/.overleaf-lean-stub

# --- Built frontend + entrypoint -------------------------------------------
COPY --from=web /web/apps/lea-standalone/dist /app/dist
COPY apps/lea-standalone/docker/entrypoint.sh /app/entrypoint.sh
COPY apps/lea-standalone/docker/prepare-mathlib.sh /app/prepare-mathlib.sh
RUN chmod +x /app/entrypoint.sh /app/prepare-mathlib.sh

WORKDIR /app

# LEA_WEB_DIST points the adapter at the built frontend so it serves the SPA on
# :8001. Model + provider key come from config/lea.local.toml (mounted volume),
# entered via the Settings pane; lea_root defaults to <repo>/prover = /app/prover.
# The companion vars point it at the in-container adapter and make it bind
# 0.0.0.0 (a 127.0.0.1 bind would be unreachable through the Docker port map);
# LEA_UI_BASE_URL is the adapter-served SPA (no Vite :5173 in the image).
ENV PYTHONUNBUFFERED=1 \
    LEA_WEB_DIST=/app/dist \
    LEA_ROOT=/app/prover \
    LEA_API_BASE_URL=http://127.0.0.1:8001 \
    LEA_UI_BASE_URL=http://localhost:8001 \
    LEA_COMPANION_HOST=0.0.0.0 \
    NODE_ENV=production

EXPOSE 8001 31245
# Intermediate runtime default; each deployable role overrides this healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8001/api/ready && curl -fsS http://127.0.0.1:31245/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

# Copy only Python dependencies, API sources and static assets into a clean
# base. Workspace/compiler/config bytes are absent from every API image layer.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS api
ARG LEA_BUILD_SOURCE=development
LABEL org.opencontainers.image.revision=$LEA_BUILD_SOURCE
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=runtime /app/adapter /app/adapter
COPY --from=web /web/apps/lea-standalone/dist /app/dist
COPY apps/lea-standalone/docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
WORKDIR /app/adapter
ENV LEA_PROCESS_ROLE=api LEA_WEB_DIST=/app/dist PYTHONUNBUFFERED=1
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8001/api/health || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]

FROM companion-node AS companion
ARG LEA_BUILD_SOURCE=development
LABEL org.opencontainers.image.revision=$LEA_BUILD_SOURCE
WORKDIR /app
COPY --from=runtime /app/apps /app/apps
COPY --from=runtime /app/packages/lea-workspace-client /app/packages/lea-workspace-client
RUN rm -f /app/apps/overleaf-extension/companion/worker.mjs \
    /app/apps/overleaf-extension/companion/workerService.mjs \
    /app/apps/overleaf-extension/companion/databaseState.mjs
ENV LEA_COMPANION_HOST=0.0.0.0 NODE_ENV=production
EXPOSE 31245
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD node -e 'fetch("http://127.0.0.1:31245/health").then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))'
ENTRYPOINT ["node", "/app/apps/overleaf-extension/companion/server.mjs"]

FROM runtime AS worker
ENV LEA_PROCESS_ROLE=worker LEA_REQUIRE_SHARED_OWNERSHIP=1
HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=3 \
  CMD /app/adapter/.venv/bin/python /app/adapter/worker_health.py || exit 1
CMD ["worker"]
