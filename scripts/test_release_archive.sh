#!/usr/bin/env bash
# Exercise the released archive as an unprivileged user with no network.
set -euo pipefail
bundle=$(realpath "$1")
platform=$2

docker run --rm --network none --platform "$platform" --user 65534:65534 \
  -v "$bundle:/bundle:ro" \
  debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 \
  sh -ec '
    cd /bundle
    sha256sum -c ./*.tar.gz.sha256
    work=$(mktemp -d)
    tar -xzf ./*.tar.gz -C "$work"
    cd "$work/suifleet"
    test -x libexec/suifleet-provision
    test -r systemd/suifleet-signer.socket
    bin/suifleet --provision "$work/device.key"
    test "$(stat -c %a "$work/device.key")" = 600
    address=$(bin/suifleet --derive-address "$work/device.key")
    case "$address" in
      0x*) test "${#address}" = 66 ;;
      *) exit 1 ;;
    esac
    test "$(bin/suifleet --derive-address "$work/device.key")" = "$address"
  '
