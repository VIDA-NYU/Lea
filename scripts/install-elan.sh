#!/usr/bin/env bash
# Install the same checksum-verified elan release on supported build hosts.
set -euo pipefail
version=v4.2.4
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) target=x86_64-unknown-linux-gnu; digest=42b94d4244e8353142c456ec0e4ca6528fd898a6c604d4059f494e706e431f63 ;;
  Linux:aarch64) target=aarch64-unknown-linux-gnu; digest=05febd124d84ebf994b2e7479922a5650b1e950c17ae3bd1ddd776b65bb72bf9 ;;
  Darwin:x86_64) target=x86_64-apple-darwin; digest=8a340b309d8ed2e96f930761fa223b3af57a38f5d253b53ac90293c9516f8cd4 ;;
  Darwin:arm64) target=aarch64-apple-darwin; digest=7ad829861392c718dfebde3a83b5c8508df47be02af68894b094b0b3952616e5 ;;
  *) echo 'Unsupported elan installation platform' >&2; exit 1 ;;
esac
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
curl -fsSL "https://github.com/leanprover/elan/releases/download/$version/elan-$target.tar.gz" -o "$temporary/elan.tar.gz"
if command -v sha256sum >/dev/null; then
  actual=$(sha256sum "$temporary/elan.tar.gz")
else
  actual=$(shasum -a 256 "$temporary/elan.tar.gz")
fi
test "${actual%% *}" = "$digest" || { echo 'elan checksum mismatch' >&2; exit 1; }
tar -xzf "$temporary/elan.tar.gz" -C "$temporary"
"$temporary/elan-init" -y "$@"
