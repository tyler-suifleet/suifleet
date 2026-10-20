#!/usr/bin/env bash
# Public dependency preparation online; all exporter/daemon traffic network-none.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SUIFLEET_DEV_IMAGE:?set pinned built dev image}"
# Match the pinned Ubuntu 22.04 container's CPython 3.10 ABI, even on a newer host.
python3 -m pip install --disable-pip-version-check --require-hashes --upgrade \
  --only-binary=:all: --python-version 3.10 --implementation cp --abi cp310 \
  --platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
  --target .ci-cache/aws-export-python -r sdk/stream-export/requirements.txt
docker run --rm --user 0:0 -v "$PWD:/workspace" -w /workspace "$SUIFLEET_DEV_IMAGE" \
  cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples -j 2
docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/workspace:ro" -w /workspace "$SUIFLEET_DEV_IMAGE" python3 scripts/test_stream_export.py
