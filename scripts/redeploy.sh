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
  echo "error: environment '$CURRENT_ENV' is not supported"
  echo "       supported: $SUPPORTED_ENVS"
  exit 1
fi

# ── Detect publish vs upgrade ──────────────────────────────────────────────────

UPGRADE_CAP=""
if [ -f "$PUBLISHED_TOML" ] && grep -q "^\[published\.$CURRENT_ENV\]" "$PUBLISHED_TOML"; then
  UPGRADE_CAP=$(grep "upgrade-capability" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
fi

# ── Run publish or upgrade (output goes to terminal) ──────────────────────────

if [ -n "$UPGRADE_CAP" ]; then
  echo "Upgrading contracts on $CURRENT_ENV (cap: $UPGRADE_CAP)..."
  sui client upgrade \
    --upgrade-capability "$UPGRADE_CAP" \
    --gas-budget 50000000 \
    "$ROOT/contracts"

  PACKAGE_ID=$(grep "published-at" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
  ORIGINAL_PACKAGE_ID=$(grep "original-id" "$PUBLISHED_TOML" | awk -F'"' '{print $2}' || echo "")
  if [ -z "$ORIGINAL_PACKAGE_ID" ]; then ORIGINAL_PACKAGE_ID="$PACKAGE_ID"; fi

  REGISTRY_ID=$(grep "NEXT_PUBLIC_REGISTRY_ID" "$ENV_FILE" 2>/dev/null | cut -d= -f2 || echo "")
  if [ -z "$REGISTRY_ID" ]; then
    echo "error: REGISTRY_ID not found in $ENV_FILE — run a fresh publish first"
    exit 1
  fi

else
  echo "Publishing contracts to $CURRENT_ENV..."

  # Show all output on the terminal; also capture the digest for event querying.
  TMPFILE=$(mktemp)
  trap 'rm -f "$TMPFILE"' EXIT

  sui client publish --gas-budget 50000000 --json "$ROOT/contracts" \
    | tee "$TMPFILE" \
    | python3 -c "
import sys, json
data = json.load(sys.stdin)
# Pretty-print the object changes so the user can see what was created
for c in data.get('objectChanges', []):
    if c.get('type') in ('created', 'published'):
        otype = c.get('objectType', c.get('type', ''))
        print(f\"  {c['type']:10s}  {c.get('objectId', c.get('packageId', '?'))}  {otype}\")
" 2>/dev/null || true

  # Parse REGISTRY_ID from captured JSON using RegistryCreated event.
  REGISTRY_ID=$(python3 -c "
import sys, json
data = json.load(open('$TMPFILE'))
# Primary: find via RegistryCreated event
for ev in data.get('events', []):
    pj = ev.get('parsedJson', {})
    if 'registry_id' in pj:
        print(pj['registry_id'])
        sys.exit(0)
# Fallback: find shared DeviceRegistry in objectChanges
for c in data.get('objectChanges', []):
    if c.get('type') == 'created' and 'DeviceRegistry' in c.get('objectType', ''):
        print(c['objectId'])
        sys.exit(0)
sys.exit(1)
" 2>/dev/null || echo "")

  if [ -z "$REGISTRY_ID" ]; then
    echo ""
    echo "error: could not determine DeviceRegistry ID from publish output."
    echo "       Check the output above and set NEXT_PUBLIC_REGISTRY_ID manually in $ENV_FILE"
    exit 1
  fi

  PACKAGE_ID=$(grep "published-at" "$PUBLISHED_TOML" | awk -F'"' '{print $2}')
  ORIGINAL_PACKAGE_ID=$(grep "original-id" "$PUBLISHED_TOML" | awk -F'"' '{print $2}' || echo "")
  if [ -z "$ORIGINAL_PACKAGE_ID" ]; then ORIGINAL_PACKAGE_ID="$PACKAGE_ID"; fi

  if [ -z "$PACKAGE_ID" ]; then
    echo "error: could not read PackageID from $PUBLISHED_TOML"
    exit 1
  fi
fi

# ── Write .env.local ───────────────────────────────────────────────────────────

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
