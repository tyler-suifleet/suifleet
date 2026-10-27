# Frontend SDK and wallet compatibility (SF-003)

The frontend client is **0.0.2**. Generated manifests admit `>=0.0.2,<0.0.3`; regenerate existing manifests/configuration before rebuilding this client. A manifest admitting only 0.0.1 fails closed.

The frontend uses `@mysten/sui` **2.36.0**, `@mysten/dapp-kit-react` **2.1.40** and its locked `@mysten/dapp-kit-core` **1.6.38**. Node **22.22.2**, pnpm **11.0.8**, React **19.3.1** and Next.js **16.4.1** remain the tested baseline. Install with the committed lockfile; upgrade SDK and wallet packages together. The SDK requires Node 22 or newer. The legacy `@mysten/dapp-kit` package and frontend JSON-RPC calls are removed.

Dependency security remediation, build-stack migration and scan evidence are in [FRONTEND_SECURITY.md](FRONTEND_SECURITY.md). Tailwind 4 requires Safari 16.4+, Chrome 111+ and Firefox 128+; validate the target extension/browser combination.

References: [Mysten React setup](https://sdk.mystenlabs.com/dapp-kit/getting-started/react), [Core querying/pagination](https://sdk.mystenlabs.com/sui/clients/querying), [SDK migration](https://sdk.mystenlabs.com/sui/migrations/sui-2.0/sui).

## Configuration and network proof

Keep using the [deployment manifest](DEPLOYMENT_MANIFEST.md). Its `rpc_url` is the fleet fullnode base URL; the browser now speaks **gRPC-Web** at that URL. The server/proxy must expose Sui v2 LedgerService, StateService, MovePackageService and TransactionExecutionService with browser CORS support. A JSON-RPC-only endpoint is insufficient. No transport fallback bypasses manifest verification. Live frontend queries were verified on a disposable **Sui 1.82.1-987123842623** node. The build image’s Sui **1.71.1** supports manifest metadata validation but returns `UNIMPLEMENTED` for `ListEvents`; its old security script therefore passes 34/35 checks, failing the frontend history read. Use a supported indexed node for gRPC acceptance. The build compiler pin remains unchanged. See [fullnode gRPC/indexing configuration](https://docs.sui.io/operators/full-node/sui-full-node).

The daemon (SF-004) and the Python publisher/resolver (SF-006) now use gRPC. Only some localnet tests still read state through a legacy JSON-RPC helper; the Sui 1.82.1 disposable node returned HTTP 404 for it, so do not use that helper as proof of current-network compatibility. This frontend migration changed neither the shared manifest schema nor any other client's transport. The gRPC genesis digest is converted to the manifest's existing eight-character chain identifier using the first four decoded bytes. Every query/submission fetches service info directly, bypassing the SDK chain-ID cache, then verifies package BCS type origins, UpgradeCap and registry type/shared version. An endpoint reset, stale upgrade or unsupported schema fails visibly.

One dApp Kit instance selects the manifest's single network. Wallet account chain support is checked before signing; account/network changes during verification require retry. The selected network and account are passed explicitly to the wallet action. Both wallet-returned effects and confirmation from the verified fleet node must indicate success. A digest or a failed transaction union is never treated as successful execution. The wallet UI loads only in the browser because its custom elements require `window`.

## Queries and layouts

Inventory uses `listOwnedObjects` and batches custodied Device objects. Groups,
policies, catalogs and deployments discover IDs through typed gRPC `ListEvents`
with a durable ascending watermark. The cursor and decoded event bytes are saved
atomically in browser local storage, scoped by chain, endpoint, call package, type
and sender. Immutable publisher/operator streams (catalogs and enrollment policies) combine
type and sender predicates. Cross-sender streams (gas draws/enrollments, groups
and deployments) retain type filters: group admins and deployment creators can
change during handover, and delegated deployments are sent by another address.
Filtering their creation events by the current operator would hide valid state.
Concurrent scans of the same stream share one request. Empty scan pages advance
the watermark; missing/repeated cursors and incomplete streams fail visibly.
Each successfully terminated page commits its watermark and discovery atomically.
At 200 pages the view shows “still loading” and resumes on the next refresh.
Persisted discovery is bounded to 256 KiB per stream and 2 MiB across this origin’s history
keys (UTF-16 size estimate). Deployment/group events retain only compact discovery IDs;
current metadata and targets come from objects. Storage limits never truncate in-memory
or returned results. Oversized streams remove their saved cursor and warn that reload
rescans the retained range; origin eviction also causes a retained-range rescan.
Cache schema v2 rejects older possibly truncated caches and rescans with a partial-history warning;
storage/quota failures warn and continue in memory instead of wedging the view.

The endpoint's `lowestAvailableCheckpoint` is checked on every scan. First reads
on a pruned endpoint show **History unavailable** with the retained boundary;
available results remain explicitly partial. A saved cursor that fell behind
retention restarts from the retained boundary, keeps known events and explicitly
warns about the gap. Invalid saved caches rescan with a partial-history warning. Missing retention metadata
also fails. Use an archival endpoint to recover complete discovery; changing the
endpoint intentionally selects a separate cache. Current group/record objects,
deployment target fields, receipts and controls are freshly read: cached events
are discovery hints only. Objects predating retained history cannot be discovered
from events on a fresh browser. SF-125's deployment inbox must be integrated only
after that API merges; this change neither guesses its interface nor claims that
older objects were recovered.

Network refresh defaults are 60 seconds (storage certification checks already
using a longer interval keep it). Budget accounts batch both deterministic
DeviceKey fields and GasAccount objects, preserving input order and validating
budget/device bindings. Fleet health batches identities, device indexes and
DeviceData objects while retaining signature/origin/binding checks. Only absent
indexes mean unenrolled/unreported; transport and dangling-object failures stay
unavailable. Legacy isolated query fixtures without `getObjects` retain the
single-object health adapter; the shipped SDK client uses batches.

The browser's SDK transport shares one per-tab request queue, spacing starts by
350 ms (at most 86 starts per 30 seconds), including SDK proof and receipt reads.
Aborted queued requests never reach the network. A 500-request queue limit fails
visibly; it does not drop work or treat it as success. Large initial views can
take longer than a refresh interval and stay loading/unavailable until verified.
This per-tab bound is not a shared-IP quota across tabs/users and does not make
public testing endpoints a production service. See the official
[public endpoint guidance](https://docs.sui.io/operators/data-management/indexer-stack-setup).

`node scripts/test_dashboard_requests.cjs` measures real query calls for 1,000
fixture devices: 45 inventory calls/proofs, 40 budget-account batches and 62
chain-health calls, including fresh service-info checks. Its conservative combined load (two inventory/health
consumers plus two network-wide 100-deployment reads, budget accounts and 30
additional calls) is 708 requests. Each deployment read costs 212 calls, including
4 target-field batches rather than 200 individual target calls; authoritative
receipts and controls still require individual reads. Cross-sender discovery
remains necessary for delegated deployments, handover and receipt history. The
queue drains the combined load in 247,450 ms of virtual time, with a peak of 86 starts in any
30-second window. This is offline query/request-scheduler evidence, not a live
endpoint throughput or production capacity measurement. The existing browser
fixtures separately exercise actual browser/wallet/health flows.


`dapp/src/lib/move-bcs.ts` defines the current contract layouts. Objects, events, UpgradeCap and receipts decode BCS rather than transport-dependent JSON. Deployment timestamps come from the actual record. Missing authoritative receipts mark legacy history unverified; legacy StatusesKey is never read. Invalid receipt authority generations, unknown statuses and object schema versions are rejected. A positive receipts version is an authority generation, so an additive upgrade may bump it without changing the client layout; protocol/schema compatibility is enforced separately by the manifest. SF-005 remains responsible for generated/compiled layout compatibility; these definitions do not by themselves prove future upgrades compatible.

On-chain deployment group targets are arbitrary IDs. History resolves only supported `DeviceGroup` objects administered by that deployment's creator; wrong types, malformed layouts, unsupported group schemas and deleted groups do not hide other deployments. Transport errors remain visible. Each unique group is read once per history query, and administrator checks apply separately per deployment. Package, UpgradeCap and registry proofs run concurrently after fresh chain verification. Wallet guards compare snapshots of the signer address and supported chain set, accepting equivalent rebuilt account objects while rejecting actual changes before signing.

## Component publishing

The [component software library](COMPONENT_LIBRARY.md) separates browsing and
guided immutable version publishing from target selection/deployment. It retains
the wallet/network/effects boundary described above.

## Verification

```bash
make dapp-install
make dapp-typecheck       # manifest, wallet boundary, receipts and query regressions
make dapp-build
# Browser Wallet Standard acceptance with a disposable fixture wallet:
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_wallet_browser.cjs
```

The browser test uses the actual dApp Kit provider/connect UI, wallet registration, transaction/effects decoder and application hook. It checks connect, successful execution, user rejection, failed effects, chain mismatch, unsupported account chain and disconnect. Network responses and the wallet are fixtures; browser and Node tests share `scripts/fixtures/manifest-client.ts`. It never accesses an existing browser profile, funds a wallet or submits to a public chain. Its bundler/browser drivers are pinned dev dependencies; only esbuild's install script is allowed. Without `CHROME_EXECUTABLE`, Playwright uses its installed Chromium.

## Real loopback gRPC acceptance

With a disposable Sui 1.82.1 node/faucet running locally:

```bash
node scripts/test_frontend_live.cjs
# Optional loopback endpoint overrides:
# FLEET_TEST_RPC=http://127.0.0.1:9000 FLEET_TEST_FAUCET=http://127.0.0.1:9123 node scripts/test_frontend_live.cjs
```

This test refuses public endpoints, uses fresh in-memory signers and an explicit empty compiler keystore, compiles offline with the pinned build compiler, publishes locally and executes the actual frontend builders. It verifies BCS inventory/group/deployment/receipt reads, group membership, deletion, and an intentional on-chain abort with failed execution effects. It also creates deployments containing a non-group Device ID and another creator's populated group, requiring readable history and no false resolved targets. Successful and failed transactions wait for checkpointed state before subsequent history reads or gas reuse. Neither an execution digest nor a preflight simulation substitutes for the failure assertion. No real wallet, Walrus upload, device or hardware is used.

The verified test-node release asset was `sui-testnet-v1.82.1-ubuntu-x86_64.tgz`, SHA-256 `112bf204fab6927b135edfb316cddac07bdba754434b5f12d021ca7e8a72858c` from the official [release](https://github.com/MystenLabs/sui/releases/tag/testnet-v1.82.1). The node ran in a disposable dev-image container, with only its binary supplied separately; normal pinned images, wallets and named volumes were not replaced.

A fixture wallet is not a real extension acceptance run. Before marking SF-003 Done, use a supported extension wallet with an explicitly authorized deployment/network: connect/reconnect, select/change accounts, reject signing, submit a successful and deliberately aborting transaction, verify resulting effects and inventory/group/deployment pagination, and check account/chain mismatch rejection. Record wallet/version, network/chain/package IDs and results without secrets. Physical Pi wallet-to-device execution remains SF-031.

## First-time wallet onboarding

See [wallet onboarding](WALLET_ONBOARDING.md) for Google zkLogin/Enoki configuration,
Slush web, session custody, recovery, first registration and device-owner transfer.
The existing manifest, authority and effects checks apply to every wallet type.

## Static wallet metadata and favicon

The static export includes `/favicon.ico` and explicitly links it from the shared
layout. Slush initialization in dApp Kit core 1.6.38 / Slush wallet 1.3.4 otherwise
fetches `https://api.slush.app/api/wallet/metadata` on every full page load. That
endpoint was reported to reject the app.suifleet.io origin through CORS; the SDK
then logs `Error fetching metadata` while retaining its fallback wallet.

We use the supported `slushWalletConfig.metadataApiUrl` option to load
`/wallet/slush.json` from the application's own origin. This is a snapshot of
Slush wallet 1.3.4's bundled fallback ID, name, icon and enabled flag (Mysten Labs,
Apache-2.0). It changes only wallet display/availability metadata; the default
`https://my.slush.app` popup origin, session, account, signing and network checks
remain the SDK's. Remote branding/disable updates are no longer fetched: review
the snapshot when upgrading the wallet SDK or when provider availability changes.
No cross-origin proxy, console suppression or credential handling is added.

After building, run `CHROME_EXECUTABLE=/usr/bin/google-chrome node
scripts/test_dapp_presentation_browser.cjs`. CI runs the same test with its pinned
headless browser and networking disabled. It discovers every app page and fails
on console errors, uncaught exceptions, external startup requests or missing
favicon/metadata at desktop and mobile widths. Public testnet price reads use
an explicit gRPC unavailable-price fixture; all other external requests fail.
This is a disconnected testnet
console walkthrough, not an actual Slush login or public-chain transaction;
manual wallet acceptance remains in SF-031/#32 and SF-087/#159.
