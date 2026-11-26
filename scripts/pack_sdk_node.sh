#!/usr/bin/env bash
# SF-302: build and pack the TypeScript/Node component SDK reproducibly.
#
# npm pack normalizes tar mtimes and owners but preserves input file modes, so
# a checkout or umask with restrictive permissions would otherwise change the
# tarball SHA-256 even though every member's content is identical. Pack from a
# private staging copy with fixed modes: 0644 for every file except the
# runnable examples (0755), and 0755 for directories.
set -euo pipefail

cd "$(dirname "$0")/../sdk/typescript"

pnpm install --frozen-lockfile
rm -rf dist ./*.tgz
node_modules/.bin/tsc -p tsconfig.json

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/package"
cp -R package.json LICENSE README.md dist examples "$stage/package/"
find "$stage/package" -type d -exec chmod 0755 {} +
find "$stage/package" -type f -exec chmod 0644 {} +
chmod 0755 "$stage/package"/examples/*.mjs

out="$PWD"
(cd "$stage/package" && npm pack --ignore-scripts --pack-destination "$out" >/dev/null)
sha256sum ./*.tgz
