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

Inventory uses `listOwnedObjects`; groups and deployment history use `listEvents`, including empty scan-limited pages with a continuation cursor. Collection cursors and history `before` cursors are separate. Repeated/missing continuation cursors fail instead of spinning. Object batches skip only missing/deleted records; transport failures propagate.

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
