#!/usr/bin/env bash
# Public dependency preparation online; all exporter/daemon traffic network-none.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SUIFLEET_DEV_IMAGE:?set pinned built dev image}"
python3 scripts/test_build_minio_fixture.py
# Match the pinned Ubuntu 22.04 container's CPython 3.10 ABI, even on a newer host.
python3 -m pip install --disable-pip-version-check --require-hashes --upgrade \
  --only-binary=:all: --python-version 3.10 --implementation cp --abi cp310 \
  --platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 \
  --target .ci-cache/aws-export-python -r sdk/stream-export/requirements.txt
# Build the isolated compatibility fixture from immutable upstream source.
# No MinIO server or data is installed on the host; the binary runs network-none.
# The checksum service can reset multiplexed HTTP/2 streams during this large
# dependency fetch. Use HTTP/1.1 for this build only; retain TLS and Go checksum
# verification and the immutable source revision. Retry only transient fetch
# failures, in the same container so verified downloads survive each attempt.
mkdir -p .ci-cache/minio-bin
docker run --rm --user 0:0 -e GOBIN=/out -e GOMAXPROCS=2 -e GODEBUG=http2client=0 \
  -v "$PWD/.ci-cache/minio-bin:/out" \
  -v "$PWD/scripts/build_minio_fixture.sh:/build-minio:ro" \
  golang@sha256:cd78d88e00afadbedd272f977d375a6247455f3a4b1178f8ae8bbcb201743a8a \
  bash /build-minio
docker run --rm --user 0:0 -v "$PWD:/workspace" -w /workspace "$SUIFLEET_DEV_IMAGE" \
  cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples -j 2
docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/workspace:ro" -w /workspace "$SUIFLEET_DEV_IMAGE" python3 scripts/test_stream_export.py
