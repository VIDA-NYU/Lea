# Shell syntax gate, without starting services.
set -eu
bash -n apps/lea-standalone/docker/entrypoint.sh apps/lea-standalone/docker/prepare-mathlib.sh scripts/prepare-ci-lean.sh
