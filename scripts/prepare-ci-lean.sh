#!/usr/bin/env bash
# Only provision the isolated CI checkout; never run application services.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v elan >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o /tmp/lea-ci-elan.sh
  sh /tmp/lea-ci-elan.sh -y --default-toolchain none
  rm /tmp/lea-ci-elan.sh
fi
elan_bin="${ELAN_HOME:-$HOME/.elan}/bin"
export PATH="$elan_bin:$PATH"
if [[ -n "${GITHUB_PATH:-}" ]]; then echo "$elan_bin" >> "$GITHUB_PATH"; fi
cd apps/lea-standalone/prover/workspace
# Retry one transient cache failure; missing targets still have to compile.
lake exe cache get || lake exe cache get \
  || echo "[ci] Mathlib cache download incomplete; compiling missing targets."
lake build Mathlib
lake build --no-build Mathlib
cd ../third_party/SafeVerify
mkdir -p .lake
if [[ ! -e .lake/packages ]]; then ln -s "$(pwd)/../../workspace/.lake/packages" .lake/packages; fi
lake build safe_verify
test -x .lake/build/bin/safe_verify
