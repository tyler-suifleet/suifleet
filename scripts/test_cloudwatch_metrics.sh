#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SUIFLEET_DEV_IMAGE:?set SUIFLEET_DEV_IMAGE to the pinned built dev image}"
mkdir -p .ci-cache/aws-python
python3 -m pip install --disable-pip-version-check --require-hashes --upgrade --target .ci-cache/aws-python \
  -r sdk/credentials/requirements.txt
docker run --rm --network none --user 0:0 -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace/.ci-cache/aws-python -v "$PWD:/workspace:ro" -w /workspace \
  "$SUIFLEET_DEV_IMAGE" python3 scripts/test_cloudwatch_metrics.py
