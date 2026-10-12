#!/usr/bin/env bash
#
# One-shot bootstrap for the Lea ecosystem.
#
# Installs the toolchains `npm run setup` assumes are already present (uv, elan),
# nudges you onto the pinned Node version, then runs setup. Idempotent — safe to
# re-run; anything already installed is left alone.
#
# Usage:
#   ./install.sh                 # full provision (UI + Overleaf)
#   ./install.sh --target ui     # only the standalone UI (skips Overleaf)
#   ./install.sh --skip-verify   # skip the SafeVerify build (faster; /verify off)
#
# Any flags are forwarded verbatim to `npm run setup`.
#
# Prefer zero local toolchain entirely? Use Docker instead:
#   cd apps/lea-standalone && docker compose pull && docker compose up

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

say()  { printf '\n\033[1m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[install] %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- uv (Python env manager) -------------------------------------------------
if have uv; then
  say "uv found: $(uv --version)"
else
  say "Installing uv (Python env manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or ~/.cargo/bin); surface it for this session.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  have uv || { warn "uv installed but not on PATH — restart your shell and re-run ./install.sh"; exit 1; }
fi

# --- elan / lake (Lean toolchain) --------------------------------------------
if have lake; then
  say "Lean toolchain found: $(lake --version)"
else
  say "Installing the Lean toolchain (elan)..."
  curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y
  export PATH="$HOME/.elan/bin:$PATH"
  have lake || { warn "elan installed but 'lake' not on PATH — restart your shell and re-run ./install.sh"; exit 1; }
fi

# --- Node (pinned via .nvmrc) ------------------------------------------------
if have nvm || [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
  # shellcheck disable=SC1090
  [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ] && . "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
  say "Selecting Node from .nvmrc via nvm..."
  nvm install >/dev/null
  nvm use >/dev/null
fi
if have node; then
  say "Node: $(node --version)"
else
  warn "Node is not installed. Install Node $(cat .nvmrc 2>/dev/null || echo 22) LTS (https://nodejs.org) and re-run."
  exit 1
fi

# --- Provision ---------------------------------------------------------------
say "Running npm run setup ${*:+(args: $*)}..."
npm run setup -- "$@"

say "Done. Add your provider API key to .env (or the app's Settings), then:"
echo "  npm run doctor        # health-check"
echo "  ./start-dev.sh        # start the stack, then open http://localhost:5173"
