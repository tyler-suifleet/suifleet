#!/bin/sh
# Funding-only tool; keep the build/test Sui 1.81.1 baseline unchanged.
set -eu
root=${1:?destination required}
mkdir -p "$root"
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
curl --fail --location --retry 3 -o "$archive" https://github.com/MystenLabs/sui/releases/download/testnet-v1.82.1/sui-testnet-v1.82.1-ubuntu-x86_64.tgz
printf '%s  %s\n' 112bf204fab6927b135edfb316cddac07bdba754434b5f12d021ca7e8a72858c "$archive" | sha256sum --check --status
tar -xzf "$archive" -C "$root" ./sui
