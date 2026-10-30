#!/usr/bin/env bash
# Runs inside the pinned Go container. Keep its verified module cache across
# bounded retries; never retry compilation errors or checksum mismatches.
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
  if (( attempt == 3 )) || ! grep -Eq 'verifying module: .*reading https://sum\.golang\.org/[^ ]+: unexpected EOF$' "$log" \
      || grep -Eq 'SECURITY ERROR|checksum mismatch' "$log"; then
    exit "$status"
  fi
  echo "Checksum-service truncated response; retrying verified fixture build ($attempt/3)" >&2
  sleep "$((attempt * 2))"
done
