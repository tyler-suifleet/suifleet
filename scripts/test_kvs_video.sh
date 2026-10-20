#!/usr/bin/env bash
# Public test-tool preparation has network; execution has no external network,
# credentials, home-directory mounts, or writable checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
video_test_tag="suifleet-video-test:$(sha256sum scripts/video-test.Dockerfile | cut -c1-16)"
docker build -t "$video_test_tag" - < scripts/video-test.Dockerfile
docker run --rm --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$PWD:/workspace:ro" "$video_test_tag" \
  python3 scripts/test_kvs_video.py -v
# Exact peer-UID proof needs disposable distinct Unix users. Capabilities are
# limited to loading private source paths and dropping to the test principals.
docker run --rm --user 0:0 --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --cap-drop ALL --cap-add DAC_OVERRIDE --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges -v "$PWD:/workspace:ro" "$video_test_tag" \
  python3 scripts/test_kvs_broker_uid.py -v
