# syntax=docker/dockerfile:1

# =============================================================================
# Lea Interface — self-contained image (frontend + UI adapter with the prover
# driven in-process + Lean 4 + Mathlib). Arch-independent: all base images and
# the elan/Lean toolchain resolve per-arch, so it builds natively on amd64 and
# arm64 hosts alike.
#
# Normal use is via docker compose (see docker-compose.yml):
#   docker compose build && docker compose up
#
# Multi-arch build+push, if you ever want to publish a prebuilt image:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/leaui:latest --push .
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: build the React frontend into static assets (dist/)
# -----------------------------------------------------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /web

COPY package.json package-lock.json ./
RUN npm ci

# Only the files the Vite build actually reads (keeps this layer small + cacheable).
COPY index.html vite.config.ts postcss.config.mjs ./
COPY src ./src
RUN npm run build   # -> /web/dist

# -----------------------------------------------------------------------------
# Stage 2: runtime — Python 3.13 + uv (base), plus Lean toolchain and Mathlib
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

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
COPY prover ./prover

# Bake Mathlib AND build SafeVerify in one layer so they share a SINGLE Mathlib.
#
# Both the workspace and SafeVerify pin the identical Mathlib commit (and the
# same transitive deps), so SafeVerify symlinks the workspace's already-baked
# `.lake/packages` instead of downloading a second multi-GB copy. The image then
# carries exactly one Mathlib. This is one RUN because the .git trim only frees
# space when it happens in the same layer that created the files.
#
# `lake exe cache get` fetches Mathlib's prebuilt oleans (no source compile). We
# do NOT `lake build` the workspace: its @[default_target] `lean_lib Lea` is
# rooted at proofs/, which is empty until the agent writes there at runtime.
# `safe_verify`, by contrast, has its own exe target and builds fine here.
WORKDIR /app/prover
RUN cd workspace \
 && lake exe cache get \
 && test -n "$(ls -A .lake/packages/mathlib/.lake/build/lib 2>/dev/null)" \
 && echo "[build] Mathlib oleans present" \
 && cd /app/prover/third_party/SafeVerify \
 && rm -rf .lake/packages && mkdir -p .lake \
 && ln -s /app/prover/workspace/.lake/packages .lake/packages \
 && lake build safe_verify \
 && test -x .lake/build/bin/safe_verify \
 && echo "[build] SafeVerify built against the shared Mathlib" \
 && rm -rf /app/prover/workspace/.lake/packages/*/.git \
 && rm -rf /root/.cache/mathlib \
 && echo "[build] one Mathlib shared; trimmed .git histories + cache"

# --- UI adapter (FastAPI) ---------------------------------------------------
# `uv sync` builds the vendored prover (lea-prover) as an editable path dep and
# installs its deps into /app/adapter/.venv — the single venv the app runs from.
# The prover is driven in-process (D1); there is no separate Lea API venv.
WORKDIR /app/adapter
COPY adapter ./
RUN uv sync   # creates /app/adapter/.venv (incl. the in-process prover)

# --- Built frontend + entrypoint -------------------------------------------
COPY --from=web /web/dist /app/dist
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

# LEA_WEB_DIST points the adapter at the built frontend so it serves the SPA on
# :8001. Model + provider key come from config/lea.local.toml (mounted volume),
# entered via the Settings pane; lea_root defaults to <repo>/prover = /app/prover.
ENV PYTHONUNBUFFERED=1 \
    LEA_WEB_DIST=/app/dist

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8001/api/ready || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
