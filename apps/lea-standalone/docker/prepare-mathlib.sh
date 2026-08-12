#!/usr/bin/env bash
# Verify the full Mathlib build before the adapter starts.  A partial cache can
# elaborate small proof files successfully and still make `suggest_imports`
# trigger an hours-long source build when its scratch file imports all of
# Mathlib.  Healthy images take only a few seconds to pass the --no-build gate.
set -euo pipefail

readonly LEA_MATHLIB_WORKSPACE_DIR="${LEA_MATHLIB_WORKSPACE_DIR:-/app/prover/workspace}"

if [[ ! -f "${LEA_MATHLIB_WORKSPACE_DIR}/lakefile.lean" ]]; then
  echo "[lea] Mathlib check failed: no Lake workspace at ${LEA_MATHLIB_WORKSPACE_DIR}." >&2
  exit 1
fi

cd "${LEA_MATHLIB_WORKSPACE_DIR}"

echo "[lea] checking the bundled Mathlib cache before starting..."
if lake build --no-build Mathlib; then
  echo "[lea] Mathlib cache is ready."
  exit 0
fi

echo "[lea] Mathlib cache is incomplete or stale; repairing it now."
echo "[lea] This is a one-time download/build and can take a while. Progress follows below."

# Prefer Mathlib's prebuilt cache.  If an artifact is unavailable upstream,
# `lake build Mathlib` below completes just that missing work from source.
if ! lake exe cache get; then
  echo "[lea] Some prebuilt Mathlib artifacts could not be downloaded; compiling the missing work." >&2
fi

lake build Mathlib

# Do not boot into reduced-capability mode.  The broad Mathlib target must be
# completely current before the LSP daemon or any cold-check fallback starts.
lake build --no-build Mathlib
echo "[lea] Mathlib repair complete. Future starts will use the persisted cache."
