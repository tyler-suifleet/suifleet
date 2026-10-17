#!/usr/bin/env bash
# Prepare public SDK dependencies online, then run all adapter traffic offline.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SUIFLEET_DEV_IMAGE:?set SUIFLEET_DEV_IMAGE to the pinned built dev image}"
mkdir -p .ci-cache/aws-python .ci-cache/aws-rust-target .ci-cache/aws-cargo
python3 -m pip install --disable-pip-version-check --require-hashes --upgrade --target .ci-cache/aws-python \
    -r sdk/credentials/requirements.txt
# No AWS credentials, host network profiles or home directories are mounted.
docker run --rm --user 0:0 \
    -e CARGO_TARGET_DIR=/workspace/.ci-cache/aws-rust-target \
    -e CARGO_HOME=/workspace/.ci-cache/aws-cargo \
    -v "$PWD:/workspace" -w /workspace "$SUIFLEET_DEV_IMAGE" \
    cargo build --locked --manifest-path sdk/credentials/rust/Cargo.toml -j 2
docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD:/workspace:ro" -w /workspace "$SUIFLEET_DEV_IMAGE" \
    python3 scripts/test_aws_credentials.py
