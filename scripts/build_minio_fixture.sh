#!/usr/bin/env bash
# Run inside the checksum-pinned Go container. Never retry compilation/checksum
# failures or replay daemon/AWS/chain operations: this only prepares public code.
set -euo pipefail
log=$(mktemp)
trap 'rm -f "$log"' EXIT
for attempt in 1 2 3; do
  if go install github.com/minio/minio@9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a >"$log" 2>&1; then
    cat "$log"
    exit 0
  else
    status=$?
  fi
  cat "$log" >&2
  # A checksum mismatch or compiler failure must not be classified as a fetch
  # outage even if another download also failed in the same invocation.
  if (( attempt == 3 )) || grep -Eq 'SECURITY ERROR|checksum mismatch|^# ' "$log" ||
     ! grep -Eq 'https://[^ ]+.*(unexpected EOF|i/o timeout|TLS handshake timeout|connection reset by peer|502 Bad Gateway|503 Service Unavailable)' "$log"; then
    exit "$status"
  fi
  echo "Transient Go dependency fetch failure; retry $attempt/2" >&2
  sleep "$((attempt * 5))"
done
