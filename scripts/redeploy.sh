#!/usr/bin/env bash
# Publish or upgrade the contracts on the active Sui CLI environment and
# generate both client configurations. Spends gas unless --dry-run is given.
# All checks and recording are in scripts/publish.py (see --help and
# docs/DEPLOYMENT_MANIFEST.md); this wrapper only builds its gRPC chain reader.
#
#   scripts/redeploy.sh --expect-address 0x... [--dry-run] [publish.py options]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cargo build --quiet --locked --manifest-path "$ROOT/suifleet-edge/Cargo.toml" --example chain_facts
exec python3 "$ROOT/scripts/publish.py" "$@"
