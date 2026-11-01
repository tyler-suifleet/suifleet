# SF-189 managed-fleet decryption grants

Codex, running model `gpt-6.1-sol`, implemented SF-189/#377 on
`work/suifleet/sf-189-95eeb04c` in the assigned worktree. No dependencies or
contract/API/layout changes. The existing `customer::policy_device` checks the
customer administrator, fleet binding, registered device and held policy cap.

The Access page now uses that fleet authority instead of requiring a wallet-owned
DeviceOwnerCap. It keeps unavailable devices in the picker with disabled options
and the activation, fleet-membership or administrator reason. It rechecks the
selected option before submission, including after authority changes.

Validation uses the pinned `suifleet-dev:latest` image, Node 22.22.2 and pnpm
11.0.8. Commands run in a disposable container mounting this checkout at
`/workspace`, with `--network none` except for frozen dependency installation:

- `pnpm install --frozen-lockfile` in `dapp`: passed.
- `make dapp-typecheck dapp-build`: passed, including its offline testnet,
  manifest, wallet and frontend regression prerequisites and static export.
- `node scripts/test_seal_frontend.cjs`: passed; owner-cap and managed devices,
  non-admin and foreign-fleet refusal, and managed transaction object arguments.
- `node scripts/test_access_browser.cjs`: passed in Chromium 156.0.8078.4;
  real Access page and builders with fixture hooks, owner-cap and managed-device
  grants, non-admin/foreign-fleet disabled reasons, loading/errors/retries,
  rejection, duplicates, revoke/rotate and responsive layout.
- Final `pnpm typecheck && pnpm build` in `dapp`: passed after the final edits.
- `git diff --check`: passed.

## Automated Testnet contract walkthrough

[sf189-testnet.json](sf189-testnet.json) records the dedicated agent account,
chain `4c78adac`, protocol 139, checksummed compiler/transaction CLI identities,
per-type origins, fixture objects, all digests, execution statuses and gas.
The public wallet configuration was copied from the explicitly authorized
`client.yaml` to a private temporary directory with only its keystore *path*
adjusted to the host mount; no key was copied. The temporary configuration lets
the CLI write its public metadata without modifying the source wallet config.

The cached disposable package/registry came from `live-state.json`; fresh reads
verified chain, registry administrator, package origins, UpgradeCap owner and
current package before writes. No publication, upgrade or original application
object mutation occurred. Machine mutations held and asserted the global
`network:suifleet-testnet` lock; coordination was posted in #11. Each PTB used
`live_common.Live` exact-byte verification/signing and the non-replenishing
`Budget`, with a 100,000,000-MIST run ceiling. Every successful transaction's
effects were independently read back.

The walkthrough created a test customer/default fleet, entitled and registered
one managed device, created a policy, granted that device decryption, read its
policy membership, then revoked and deregistered it. The first 30M-gas setup PTB
failed with `InsufficientGas`; its failed effects charged 1,000,947 MIST and were
retained. Continuation reused the created customer and original allowance,
raising only that setup transaction's reservation to 60M. It did not replay
customer creation or replenish the run allowance. All later steps succeeded.
Net balance reduction: 39,464,621 MIST. Conservative allowance consumed:
55,857,530 MIST. No faucet or device funding was needed. The lock was released.
The empty customer/fleet/policy remain as a recorded test fixture because these
objects have no delete API; the transient device and grant were removed.

The testnet walkthrough exercised the existing contract path using the CLI;
the browser tests separately exercised the real page and transaction builders.
This is not a hosted-site/manual extension-wallet or production Seal-provider
acceptance claim. Those final external checks, physical hardware and actual
Mainnet remain unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Hosted CI is pending on the pushed PR head. Independent exact-head/current-base
review, ordinary/security CI and applicable automatic Testnet remain coordinator
merge gates. No deployment or migration is required by this frontend change.
