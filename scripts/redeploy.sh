#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR/.."
ENV_FILE="$ROOT/dapp/.env.local"
PUBLISHED_TOML="$ROOT/contracts/Published.toml"

SUPPORTED_ENVS="devnet testnet"

CURRENT_ENV=$(sui client active-env 2>/dev/null || echo "")

if [ -z "$CURRENT_ENV" ]; then
  echo "error: no active SUI environment. Run: sui client switch --env <env>"
  exit 1
fi

if ! echo "$SUPPORTED_ENVS" | grep -qw "$CURRENT_ENV"; then
  echo "error: environment '$CURRENT_ENV' is not supported for deployment"
  echo "       supported: $SUPPORTED_ENVS"
  exit 1
fi

# Detect whether the package has already been published in this environment.
UPGRADE_CAP=""
if [ -f "$PUBLISHED_TOML" ] && grep -q "^\[published\.$CURRENT_ENV\]" "$PUBLISHED_TOML"; then
  UPGRADE_CAP=$(grep "upgrade-capability" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
fi

if [ -n "$UPGRADE_CAP" ]; then
  echo "Upgrading contracts on $CURRENT_ENV (cap: $UPGRADE_CAP)..."
  sui client upgrade \
    --upgrade-capability "$UPGRADE_CAP" \
    --gas-budget 200000000 \
    "$ROOT/contracts"

  PACKAGE_ID=$(grep "published-at" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
  ORIGINAL_PACKAGE_ID=$(grep "original-id" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
  if [ -z "$ORIGINAL_PACKAGE_ID" ]; then ORIGINAL_PACKAGE_ID="$PACKAGE_ID"; fi

  # Registry persists across upgrades — keep the existing value.
  REGISTRY_ID=$(grep "NEXT_PUBLIC_REGISTRY_ID" "$ENV_FILE" 2>/dev/null | cut -d= -f2 || echo "")
  if [ -z "$REGISTRY_ID" ]; then
    echo "error: REGISTRY_ID not found in $ENV_FILE — run a fresh publish first"
    exit 1
  fi

else
  if [ "$CURRENT_ENV" = "testnet" ]; then
    echo "Publishing contracts to $CURRENT_ENV..."
    PUBLISH_JSON=$(sui client publish --gas-budget 200000000 --json "$ROOT/contracts")
  else
    echo "Publishing contracts to $CURRENT_ENV (dry-run)..."
    PUBLISH_JSON=$(sui client test-publish --build-env "$CURRENT_ENV" --gas-budget 200000000 --json "$ROOT/contracts")
  fi

  # Parse the DeviceRegistry shared object ID from publish output.
  REGISTRY_ID=$(echo "$PUBLISH_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
changes = data.get('objectChanges', [])
for c in changes:
    if c.get('type') == 'created' and 'DeviceRegistry' in c.get('objectType', ''):
        print(c['objectId'])
        break
" 2>/dev/null || echo "")

  if [ -z "$REGISTRY_ID" ]; then
    echo "error: could not parse DeviceRegistry object ID from publish output"
    echo "       Check the transaction output and set REGISTRY_ID manually in $ENV_FILE"
    exit 1
  fi

  PACKAGE_ID=$(grep "published-at" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
  ORIGINAL_PACKAGE_ID=$(grep "original-id" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
  if [ -z "$ORIGINAL_PACKAGE_ID" ]; then ORIGINAL_PACKAGE_ID="$PACKAGE_ID"; fi
fi

if [ -z "$PACKAGE_ID" ]; then
  echo "error: could not read PackageID from $PUBLISHED_TOML"
  exit 1
fi

echo ""
echo "Writing $ENV_FILE..."
cat > "$ENV_FILE" <<EOF
NEXT_PUBLIC_PACKAGE_ID=$PACKAGE_ID
NEXT_PUBLIC_ORIGINAL_PACKAGE_ID=$ORIGINAL_PACKAGE_ID
NEXT_PUBLIC_REGISTRY_ID=$REGISTRY_ID
NEXT_PUBLIC_NETWORK=$CURRENT_ENV
EOF

echo ""
echo "╔══════════════════════════════════════╗"
echo "║  SUCCEEDED                           ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  NETWORK             = $CURRENT_ENV"
echo "  PACKAGE_ID          = $PACKAGE_ID"
echo "  ORIGINAL_PACKAGE_ID = $ORIGINAL_PACKAGE_ID"
echo "  REGISTRY_ID         = $REGISTRY_ID"
echo ""
echo "Restart the dApp dev server to pick up the new IDs."
