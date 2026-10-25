# Testnet accounts and live gates

For exact encrypted artifact/recipe sizes, historical and current transaction
effects, signed cleanup rebates, token reconciliation and explicit unquoted
production costs, see the [SF-054 fleet cost model](business/FLEET_COST_MODEL.md).
Its Mainnet reads and sponsored Testnet storage observations are separate;
neither is a complete production invoice. Gas allowances below are spending
controls and must not be reported as consumed fees.

## Accounts

The user created two dedicated **testnet-only** accounts on 2026-10-14 and authorized agents to spend their testnet SUI on acceptance runs: publishing and upgrading test packages, and the transactions those runs need.

| Account | Address | Key lives in | Used by |
|---|---|---|---|
| agent | `0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965` | `~/.config/suifleet/testnet/agent/` on the development machine | Claude and Codex sessions on that machine |
| ci | `0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac` | the GitHub environment secret `SUIFLEET_TESTNET_KEY` (environment `live-package-upgrade`) | `.github/workflows/live-gates.yml` |

**Separate keys.** The two accounts have separate keys, so exposure of one cannot spend the other. They hold valueless testnet SUI only.

**Rules:**
- Never use these keys on mainnet.
- Never print, log or commit a key or recovery phrase. Never copy the CI key to the machine wallet, or the reverse.
- Use the wallet only through its `client.yaml`: `sui client --client.config ~/.config/suifleet/testnet/agent/client.yaml …`, or `SUIFLEET_WALLET=…` for make targets. Never through the default `~/.sui` wallet, which `make down` deletes.
- **Recovery phrases.** Each account directory holds a `RECOVERY_PHRASE.txt` (mode 0600) for the user to move to a password manager.
- **Directory permissions.** Keep the directory 0700.
- **Spend.** Live runs check the chain identity, the dedicated wallet and the balance first. Every mutation is charged to a strict allowance (`--max-spend-mist`; the package gate defaults to 0.8 SUI; `scripts/testnet_budget.py`): its gas budget and any SUI it sends to a run-owned account (test device, browser test key) are reserved before execution and settled from the effects. Failed and aborted transactions are charged; an unknown outcome keeps its whole reservation; rebates, faucet credits and sweeps never replenish it. A funded account's own transactions are bounded by its funding. See Cost below: a typical run costs about 0.0025 SUI.
- **Record each run.** Write the transaction digests and the evidence JSON into the PR or issue. Never record keys.

## Funding

Testnet SUI comes from the faucet's proof-of-work endpoint, which needs **Sui CLI ≥ 1.82.1**:

```bash
sui client faucet --address 0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965
# "Request successful. The faucet sent 1 SUI to the address balance of …"
```

**Older CLIs don't work.** The pinned 1.81.1 CLI only redirects to the web faucet, and `POST /v2/gas` answers "CI compatibility is disabled".

**Funding uses the pinned transaction CLI.** The dev image installs the checksum-verified 1.82.1 release binary at `/opt/sui-network/sui` (see [Toolchain](#toolchain-release-compiler-and-transaction-cli)), and live runs request funds only through that verified binary. The faucet credits the *address balance*, which both CLIs can spend as gas.

**The web faucet** is the alternative: `https://faucet.sui.io/?address=ADDRESS`.

## Toolchain: release compiler and transaction CLI

Live runs use two pinned Sui CLIs with separate jobs ([`scripts/sui_toolchain.py`](../scripts/sui_toolchain.py)):

| Role | Pin | Supports protocol | Used for |
|---|---|---|---|
| Release compiler | `sui` on PATH, Dockerfile `SUI_VERSION` = `testnet-v1.81.1` (`1.81.1-e04cf9aa915c`) | up to 138 | offline `sui move build`, Move tests, the SF-050 lint/security baseline and all package bytecode |
| Transaction CLI | `/opt/sui-network/sui`, Dockerfile `SUI_NETWORK_VERSION` = `testnet-v1.82.1` (`1.82.1-987123842623`); archive sha256 `112bf204…2858c`, binary sha256 `de1d7eff…a0d6b` | up to 139 | every `sui client` call: reads, PTBs, the faucet, and building, signing and submitting publish/upgrade transactions |

The protocol limits are `MAX_PROTOCOL_VERSION` in `crates/sui-protocol-config/src/lib.rs` at each release tag. Neither version string alone proves compatibility; the runs below do.

**Why the split.** Since testnet moved to protocol 139, `sui client test-upgrade` from 1.81.1 fails with `protocol version 139 is newer than the maximum version 138 supported by this CLI`. Its client-side upgrade compatibility check needs the network's protocol config. Publish and plain PTBs don't, so cached runs kept passing while every fresh fixture with differing contracts published its base package and then failed at the upgrade. A free `--dry-run` reproduces this on testnet: 1.81.1 fails, and 1.82.1 passes the same upgrade.

**What each run checks:**
- **Pinned identities, before anything else.** The release compiler's exact version, and the transaction CLI's exact version and binary SHA-256. A different `sui` on PATH, or an unpinned transaction binary, stops the run.
- **Read-only preflight before any faucet request or write.** The gRPC chain reader (`chain_facts`) reports the chain identifier, epoch and current protocol version. A protocol newer than the transaction CLI supports stops the run before it spends anything. Pin a newer transaction CLI, with its own evidence, instead of disabling the check.
- **Verify before signing.** For publish and upgrade:
  1. The release compiler builds the bytecode offline. `Move.lock` must not change.
  2. The transaction CLI builds the unsigned transaction (`--serialize-unsigned-transaction`), including its upgrade compatibility check. It must not change `Move.lock` either.
  3. The pinned frontend SDK decodes the transaction ([`sui_tx_inspect.cjs`](../scripts/sui_tx_inspect.cjs)). The run requires byte-identical modules and dependencies, the authorized upgrade digest, the sender, gas owner and budget, and the exact command shape: publish plus a transfer of its UpgradeCap to the wallet, or authorize/upgrade/commit with this run's UpgradeCap and package.
  4. Only those bytes are signed and executed (`sui client serialized-tx`).

  A refusal before signing releases its reservation and is recorded as `not_submitted`. Everything submitted is charged as before.
- **Provenance.** Evidence records both CLIs (version, path, protocol limit, binary checksum), the chain protocol and epoch, and the `Move.lock` framework sources for base and head. Each new fixture records the commits, both CLIs, the protocol, the framework pins and the verified transaction digests. Fixture keys include the release compiler version as well as the base/head contract hashes. A fixture is never reused for another pair or compiler, and a compiler change republishes once.

**SF-056 verification (2026-10-16).**
- **Reproduced for free.** On testnet (chain `4c78adac`, epoch 1254, protocol 139), a `--dry-run` upgrade of a cached fixture fails with 1.81.1 and succeeds with 1.82.1. A disposable 1.82.1 localnet (protocol 139) reproduces the same split offline.
- **Real acceptance with the agent wallet.**
  - Fresh base `3ff488c` → head `b81ff39`: all checks passed, including the upgrade (`HLp3bSDEQ2VKZwXfcyZiE8MXJt2MvyRdDnxXQwoPx6H8`). Net cost 411,486,525 MIST, against a 460,915,082 MIST conservative allowance and one faucet request.
  - Cached rerun: three transactions, 2,001,843 MIST.

Digests and effects are in [the SF-056 receipt](validation/sf056-testnet.json). These runs used the dedicated agent wallet, not the CI wallet. The automatic CI run on the PR head records its own evidence.

**Updating the transaction CLI.** When testnet moves past protocol 139, update `SUI_NETWORK_*` in the Dockerfile and `NETWORK` in `sui_toolchain.py` together, with the release's protocol limit and checksums. The tests in `scripts/tests/test_testnet_toolchain.py` check that the Dockerfile and the constants agree, and inside the dev image they check the installed binary's identity. Changing the release compiler is a separate, reviewed toolchain upgrade (the SF-050 baseline), not a fix for network protocol drift.

## SF-083 fleet budget run

`make test-budget-testnet SUIFLEET_WALLET=…` (`scripts/live_fleet_budget.py`) runs on the same executor and allowance. It covers the fleet budget, draws from the device's own gas account with the budget left at its version, refusals, real daemon draws, a SUI→WAL swap, storage bought and extended with the fleet's WAL, a device's fleet-paid Walrus upload and an owner upload through the dApp's own upload code (`scripts/live_fleet_upload.cjs`, each transaction decoded and verified before the wallet signs it). It caches its package fixture in `sf083-state.json` beside the wallet and keeps a pending device key until the key is swept. It needs the machine-session lock token and the pinned Walrus SDK installed privately. See [FLEET_BUDGET.md](FLEET_BUDGET.md) and [sf083-testnet.json](validation/sf083-testnet.json).

## The `package-upgrade` live gate

```bash
make live-gate-package-upgrade SUIFLEET_WALLET=$HOME/.config/suifleet/testnet/agent/client.yaml [BASE_REF=origin/main]
```

`scripts/live_package_upgrade.py`:
1. Publishes the **base** contracts (default: the merge base with `origin/main`) as a fresh ephemeral publication.
2. Creates a device (key provisioned by the daemon), its cap and a group with that package, then activates it into registry custody through the head package.
3. Upgrades in place to the checked-out tree with the run's own UpgradeCap.
4. Verifies the manifest over gRPC.
5. Runs the real daemon with the SF-030 test updater and a local Walrus stand-in (`SUIFLEET_WALRUS=testnet`: real Walrus testnet, the publisher/aggregator in the manifest), and deploys:
   - through the new package, to the device and to the pre-upgrade group;
   - through the **old** package's entry point (mixed versions);
   - an injected health failure.
6. Checks every receipt on chain with the Sui CLI, independently of the daemon, requiring current authority version 2.
7. Deletes the deployments for the storage rebate.
8. Writes evidence: tool versions, chain, packages, type origins, each transaction digest and status, receipts and SUI spent.

**Automatic CI.** `.github/workflows/testnet.yml` is sourced from main and runs after successful `CI` completion, daily at 08:23 UTC, and through main-only dispatch. The controller re-fetches originating workflow facts, verifies CI identity/success, checks PR author and run actor write authority, rejects forks, and binds the exact current PR SHA and current main base. Superseded runs are skipped before spending. Main push/periodic checks compare main with its first parent. Public fork CI remains unprivileged/offline. No arbitrary dispatch ref receives secrets.

`live-package-upgrade` is configured main-only, with no required reviewers and `can_admins_bypass=false`; this is the user's explicit standing testnet authorization. The planner verifies these settings; the runtime derives the address from the protected secret and requires the dedicated CI address. All automatic/manual testnet paths share concurrency `suifleet-testnet-ci-wallet`, `cancel-in-progress=false`, `queue: max` (up to 100 pending runs; queue overflow fails closed through missing required status). The old manual package gate delegates to this controller; wallet/hardware approvals remain unchanged.

The controller sets `testnet/package-upgrade` on the tested SHA, with `base:<exact base SHA>` in its description. A failure remains a failing job and status. The sole orchestrator requires this success for the current head/base once `testnet.yml` exists on main; the first reviewed workflow merge bootstraps before activation. Install the reviewed coordinator after merge. Missing status blocks later integration even before GitHub queues the privileged run. This testnet gate does not reinstate unrelated deferred integration blockers.

**Spending.** The package gate default allowance is 800,000,000 MIST; a caller may set a lower ceiling. Every publish, upgrade, PTB, funding transfer and cleanup uses one executor. It caps each transaction gas budget at remaining allowance, counts nonnegative gas on both successful and failed effects, reserves the full budget on unknown execution outcomes, and counts all available device gas plus new funding. Refunds/faucet credits never increase allowance. Abort cleanup uses only what remains; evidence survives failures. This is a conservative ceiling, so accounted allowance may exceed net wallet spend.

**Automatic funding.** If the dedicated wallet holds less than the run allowance, the runner may make one testnet faucet request, bounded to 180 seconds, through the verified transaction CLI already in the image. Compilation and tests remain on Sui 1.81.1. Faucet outages and insufficient balance fail clearly. No user hand-funding step is part of routine CI.

**Cost.** Package storage is never refunded, so the published fixture is cached. The fixture is the base package, its upgrade, the device with its key, the cap and the group.
- **Where the cache lives.** Locally it's `live-state.json` beside the wallet; on CI only AES-256-GCM ciphertext is stored in the main-workflow encrypted fixture artifact. The domain-separated encryption key is derived through HKDF-SHA256 from the protected CI secret; the CI key itself is never cached. Decrypted device state and public wallet configuration exist only in a mode-0700 temporary directory, files mode 0600, removed on exit. CI signing bytes travel through an in-process pipe to a Linux anonymous memfd; a private runtime symlink permits the pinned CLI to read it without a disk key file or secret command arguments. Tampering and wrong keys fail closed. It is keyed by exact pinned-compiler root-package bytecode digests for the base/head pair; separately compiled storage/data packages and test-only changes do not trigger core republication. Compiler/framework-bound matching completed cache entries may migrate only after free ownership/existence verification.
- **Re-verified before reuse.** Free reads check that every object still exists with the expected owner.
- **Fresh fixture** (first run, or after any contract change): about 0.58 SUI when base and head differ (SF-083: base publish 0.265 + head upgrade 0.316 SUI). Earlier, smaller packages cost about 0.41 and then 0.18 SUI.
- **Cached run:** about 0.0025 SUI. It sends three transactions: head-package creates, a base-package create (one transaction cannot call two versions of a package), and one delete batch that recovers the storage.
- **Identical contracts.** When base and head contracts are identical, the upgrade is skipped.
- **Device gas.** The device's report gas, about 0.004 SUI per run, is topped up with 0.03 SUI only when below 0.01. So 1 SUI covers hundreds of cached runs.
- **Fresh fixture safety.** `--fresh` refuses republication when matching verified bytecode already exists. A broken fixture or pending/unknown setup stops for reconciliation instead of republishing. Successful setup steps checkpoint their IDs and verified bytecode privately, so retries reuse completed publications. A verified chain or dedicated-wallet change selects a separate state namespace.
- **Device key.** This is a signing credential: disclosure permits receipt spoofing/griefing. Never publish plaintext state in caches, artifacts or logs.

**Limits:**
- Walrus is a local stand-in by default (automatic CI); pass `SUIFLEET_WALRUS=testnet` for real Walrus storage.
- `scripts/redeploy.sh` / `scripts/publish.py` still submit through the PATH `sui` (1.81.1). On a protocol-139 network, `publish.py` upgrades would hit the same client-side check. They are not yet run against testnet (SF-006 acceptance #11) and need the same split before they are. The SF-010 suites below use the split toolchain.
- No old-package attacks or legacy objects: that is the upgrade-security suite below. No browser wallet: that is the `wallet` gate below.
- There is no hardware: that is the `hardware` gate (SF-031).
- The device keeps its unused report gas, about 0.03 SUI per run.
- **No JSON-RPC.** Public testnet fullnodes no longer serve JSON-RPC (gRPC and GraphQL only). Live tooling must read over gRPC (`chain_facts`, the daemon, the frontend SDK) or through the Sui CLI. The localnet tests' JSON-RPC helper does not work there.

## Upgrade-security suite (SF-010)

```bash
make live-gate-upgrade-security SUIFLEET_WALLET=$HOME/.config/suifleet/testnet/agent/client.yaml
```

`scripts/live_upgrade_security.py` proves on a public network what the Move tests prove locally: existing objects survive an upgrade, later-introduced types originate in the upgrade, and older package versions — which stay callable forever — cannot be used to bypass the new authority. Recipes and artifacts go to real Walrus testnet. Uploads have three total attempts: the Mysten reference publisher, then the HTTPS H2O Nodes and n1stake Testnet publishers listed in the [official operator list](https://docs.wal.app/docs/system-overview/public-aggregators-and-publishers). Only timeouts, connection failures and HTTP 5xx advance to another publisher; client errors fail immediately. All attempts send identical bytes, and success still requires exact-byte readback through the configured aggregator. Exhaustion fails the run; no wallet transaction is replayed or spending limit increased.

The base package and its upgrade to head are cached in the fixture state (`live-state.json` beside the wallet, `security_fixtures`), keyed by the content hashes of the base and head contracts and the release compiler (as the package-upgrade fixture is). Every `sui client` command runs on the pinned transaction CLI after the read-only protocol check, and the publish and upgrade sign only bytecode verified against the release compiler's output (`scripts/sui_toolchain.py`). Before reuse the suite re-reads the UpgradeCap — still the operator's, at version 2 and pointing at the cached head, so no other upgrade was chained on top — and the shared registry, and the manifest's type origins must still split between base and head. A reused pair creates the legacy state through the base package's entry points, which older packages keep after an upgrade; the objects and types are the same as those created before it. A cached run costs about 0.03 SUI net. The allowance is charged about 0.105 SUI, because the device's 0.04 SUI of funding counts in full even though most of it is swept back. `FRESH=1` (`--fresh`) publishes a new pair (about 0.22 SUI) so the legacy state is created before the upgrade; do that for release candidates and contract-security changes.

1. Publishes the pre-identity contracts `14b351b` (before SF-008), or reuses the cached pair, and creates with them a **legacy** device, its bound cap, a group, and a deployment the device reports Applied through the old `report_status`.
2. Upgrades in place to the checked-out tree and verifies the manifest: identity types (`IdentityKey`, `DeviceOwnerCap`, …) originate in the upgrade, existing types in the base.
3. Attacks, each required to abort on chain with the exact module, function and code, and to leave no receipt:

| Attack | Expected abort |
|---|---|
| Device reports on a head-created deployment through the **old** package's `report_status` | `deployment_manager::receipts_mut` 5 |
| Device calls the superseded `report_status` in the **new** package | `deployment_manager::report_status` 7 |
| `report_verified_status` before the legacy device is activated | `device_cap::assert_active` 2 |
| Operator reports with a cap it minted for the device (impersonation) | `device_cap::assert_signer` 0 |

4. Activates the legacy device through the head package; the real daemon applies a new deployment (artifact from Walrus testnet) and its pre-upgrade cap reports Applied.
5. Revokes the device; its next verified report aborts (`device_cap::assert_active` 2) and the receipt is unchanged.
6. Cleans up, after a failure too. It deletes the deployments and the group and deregisters the device for the storage rebate. An activated device leaves a revoked identity tombstone, and the two DeviceCaps have no delete function, so they remain. Each cleanup transaction has a 0.02 SUI budget, so an unknown outcome cannot starve the rest.
7. Sweeps the device key's unspent funding back to the operator. The sweep is paid from the device's charged funding and never replenishes the allowance. The key's seed is kept (0600) beside the wallet as `live-security-device.key` from before the funding until that sweep succeeds. The next run sweeps a leftover key first, charged to its own allowance.
8. Writes evidence: abort locations, digests, receipts, type origins, cleanup failures and SUI spent and swept.

## The `wallet` live gate (SF-010)

```bash
make live-gate-package-upgrade SUIFLEET_WALLET=$HOME/.config/suifleet/testnet/agent/client.yaml   # creates/refreshes the fixture
make live-gate-wallet SUIFLEET_WALLET=$HOME/.config/suifleet/testnet/agent/client.yaml
```

`scripts/live_wallet_gate.py` bundles `scripts/browser/live-acceptance.jsx` with the dApp's real dApp Kit provider, `ConnectButton`, `useFleetTransaction` hook, transaction builders, queries and Walrus upload code, and drives it in headless Chromium (`scripts/live_wallet_browser.cjs`) against Sui testnet and Walrus testnet. It reuses the package-upgrade fixture's package and registry (no publish), so a run costs about **0.018 SUI**.

The wallet is a scripted Wallet Standard wallet signing with a throwaway ed25519 key, the one stand-in for an extension. The key is funded with 0.1 SUI and swept back at the end. The funding is charged to the allowance in full (default 0.2 SUI); the sweep never replenishes it. Until the sweep succeeds its seed is kept (0600) beside the wallet as `live-browser-key.hex`; the next run sweeps it first, reserving that sweep's gas against its own allowance (the earlier run already charged the funding).

Checks:
- connect through the dApp Kit connect UI;
- wallet rejection surfaces as an error, nothing executed;
- fresh registration, cap issuance and activation (owner cap held by the wallet), listed by `fetchDevices`;
- group creation and membership of the activated device;
- recipe and artifact uploaded to Walrus testnet by the dApp; the verified deployment is listed with its resolved device and both blobs probe available;
- an abort refused by simulation before signing, with nothing executed;
- an abort **executed on chain** (fully resolved transaction, so the SDK does not simulate) and reported as failed by the hook; the digest's failure status is read back from the network;
- an account without the manifest's chain is refused before signing;
- cleanup through the dApp (deployment, membership, group, device) and the sweep.

## Extension-wallet check

A real extension (Slush or another Wallet Standard wallet) is not part of the automatic suites. Run this check per release candidate, either by hand or by driving the extension's own popups as in the 2026-10-19 run below:

1. Run the package-upgrade gate, then resolve its fixture into a frontend manifest (free reads; IDs from `live-state.json` or the gate's evidence):
   ```bash
   python3 scripts/deployment_manifest.py resolve --network testnet --rpc https://fullnode.testnet.sui.io:443 \
     --chain-identifier 4c78adac --package <head_package> --upgrade-cap <upgrade_cap> --registry <registry> \
     --publisher https://publisher.walrus-testnet.walrus.space --aggregator https://aggregator.walrus-testnet.walrus.space \
     --output /tmp/deployment.json
   printf '[device]\n[apply]\n' > /tmp/device.toml   # the browser only needs frontend.env
   python3 scripts/deployment_manifest.py generate /tmp/deployment.json --output /tmp/bundle --device-template /tmp/device.toml
   cp /tmp/bundle/frontend.env dapp/.env.local && make dapp-dev
   ```
2. In a browser profile with the extension on **testnet** and a little testnet SUI, at `http://localhost:3000`:
   - connect; the address appears;
   - reject a signing request: an error, nothing executed;
   - register a device, then create a group and add it;
   - upload a small artifact and create a deployment to the group; it lists with its device;
   - switch the extension to mainnet/devnet: the dApp still requests `sui:testnet` (the manifest network), and the wallet either refuses or executes on testnet. Nothing may execute on another chain;
   - disconnect and reconnect;
   - delete the deployment, group and device.
3. Record the extension name/version, browser, date and the digests in #11.

**Run of 2026-10-19 (Slush).** The run used Slush 26.21.4 from the Chrome Web Store, in Chrome for Testing 156.0.8078.4 (`--headless=new`). Playwright clicked through Slush's own onboarding, connection and transaction popups. The dApp was this branch's `dapp/` (`next dev`), using the CI package-upgrade fixture `0x57ff9cde…` for these contracts. The Slush account was a throwaway key, funded with 0.1 SUI from the agent wallet and swept back afterwards. All checks passed:
- rejecting the connection gave "Connection failed";
- rejecting a signature gave "Registration failed: User rejected the request." and nothing executed;
- registration, cap and activation ran as one PTB; Slush's preview showed the `DeviceOwnerCap` going to the wallet;
- the Issue cap action, group creation and membership worked;
- the dApp uploaded the artifact and recipe to Walrus testnet; the verified deployment was listed with its resolved device;
- disconnect and reconnect (with a new approval) worked;
- cleanup deleted everything.

Slush accounts advertise `sui:mainnet`, `sui:devnet` and `sui:testnet`, so the dApp's missing-chain refusal cannot trigger with Slush. Switched to Mainnet, Slush warned that "this transaction will process on testnet", and the approved transaction executed and was confirmed on testnet. The refusal itself, simulation refusals and executed aborts remain covered by the scripted wallet gate.

The run made 11 extension transactions, all successful, costing 0.0213 SUI of the test key's gas; the agent wallet's net cost was 0.0243 SUI. Digests are in [`validation/sf010-testnet.json`](validation/sf010-testnet.json) under `extension_wallet_slush`.

## SF-010 suites in automatic CI

The automatic main-only controller (`testnet.yml`, environment `live-package-upgrade`, concurrency `suifleet-testnet-ci-wallet`, the dedicated CI wallet) runs both SF-010 suites inside the same key-bearing step, after the package-upgrade gate passes (`scripts/ci_testnet_wallet.py`):

- **When.** They run when the authenticated head changes anything they test against the base (`contracts/`, `dapp/`, `interfaces/`, `suifleet-edge/`, `scripts/browser/`, `scripts/live_*`, `scripts/ci_testnet*`, the manifest resolver, the budget or the `Dockerfile`), and on a manual dispatch. The manual `wallet` live gate now dispatches this controller, as `package-upgrade` already did. Scheduled main runs skip them. A suite failure fails the exact-head `testnet/package-upgrade` status.
- **Allowances.** Each suite has its own strict allowance: upgrade-security 0.7 SUI, wallet 0.15 SUI (0.102 with the browser funding charged). A fresh upgrade-security pair used 0.348 SUI at `96c441e`; with SF-022's Seal policy module the upgrade alone costs 0.241 SUI, and a fresh pair stopped at 0.399 of the former 0.4 SUI allowance before its last deployment and cleanup (run 39343914188). With SF-083's fleet budget, a fresh base→head pair costs about 0.58 SUI (base publish 0.265 + head upgrade 0.316), which exhausted the former 0.6 SUI package allowance before activation (run 39720082567). The package gate therefore allows 0.8 SUI and upgrade-security 0.7 SUI. A run's ceiling is 1.65 SUI; cached runs still cost a few thousandths. Only the package gate may request the faucet.
- **State.** The upgrade-security package pair is cached in the same encrypted fixture state, under `security_fixtures`. The wallet gate reuses the package-upgrade fixture just verified. A test key that a suite could not sweep is kept in that encrypted state under `pending_keys` and recovered by the next run, charged to that run. If the job is killed before the state is sealed, such a key is lost, with at most its 0.1 or 0.04 SUI funding.
- **Browser.** The dev image pins the Chrome for Testing headless shell that Playwright 1.64.1 drives (156.0.8078.4, sha256-checked) and sets `CHROME_EXECUTABLE`. The browser runs only from the installed `dapp` dependencies; nothing is installed in the key-bearing step.
- **Evidence.** The workflow uploads `evidence/*.json` (`package-upgrade`, `upgrade-security`, `wallet`), and none of it contains keys. The uploaded workflow file comes from main, so until this change is integrated the run's log is the only record of the two suites.

## SF-010 acceptance matrix

| Acceptance | Where | Status |
|---|---|---|
| Fresh registration, cap issuance | wallet gate (dApp) | testnet PASS |
| Recipe upload | wallet gate (dApp → Walrus testnet); upgrade gates (`SUIFLEET_WALRUS=testnet`) | testnet PASS |
| Direct and group deployment, real execution effects | package-upgrade gate (real daemon, receipts read independently, effects re-read over gRPC) | testnet PASS |
| Existing objects and later-introduced types across an upgrade | upgrade-security suite | testnet PASS |
| Legacy caps and old-package attacks | upgrade-security suite (4 attacks, exact aborts) | testnet PASS |
| Revocation | upgrade-security suite | testnet PASS |
| Wallet rejection, failure, wrong chain | wallet gate (scripted wallet) | testnet PASS |
| Real browser-extension wallet | extension-wallet check above (Slush 26.21.4, Chrome for Testing 156, 2026-10-19) | testnet PASS |
| Real device | `hardware` gate (SF-031) | **pending hardware** |

Evidence (no keys): [`validation/sf010-testnet.json`](validation/sf010-testnet.json).

## Cost findings

These were measured on Sui testnet on 2026-10-15 with Sui CLI 1.81.1 and this repository's package. They apply to any live-network work: acceptance gates, release rehearsals and frontend live tests.

| Item | Measured | Consequence |
|---|---|---|
| Publish or upgrade of `sui_edge` | about 0.087 SUI of storage each, **never refunded** (packages are immutable) | The dominant cost. Reuse packages across runs, keyed by contract content, and skip the upgrade when base and head are identical |
| Creating objects (deployments, groups, devices) | storage charged up front, **about 99% rebated on deletion** | Delete transient objects in the run's cleanup. A delete batch returned more than its two creates cost (net −0.049 SUI) |
| Any transaction | a minimum computation fee of about 0.001 SUI at the reference gas price | Batch calls into one PTB: 8 single-call transactions became 3 |
| A PTB calling two versions of one package | rejected with `InvalidLinkage`, and the failed execution still charged about 0.001 SUI | Group calls by package version: one transaction for head calls, one for base calls |
| Gas budget | not spent; reserved from the balance at selection | A 0.9 SUI budget on a 0.89 SUI wallet failed at the upgrade, after the publish had already spent 0.11 SUI. Keep budgets realistic, and check the balance against the whole run before starting |
| Package upgrades | chain linearly from the latest version only (the UpgradeCap's current package) | A cached fixture can only test the exact (base, head) pair it was built for. Upgrading it again would test head₁→head₂, not base→head₂ |
| Device report gas | about 0.001 SUI per status report, paid by the device | Fund the device once and top it up below a floor. Don't send fresh funding each run (that lost about 0.03 SUI per run) |
| Faucet | Sui ≥ 1.82.1 `client faucet` solves a proof of work: 1 SUI per request, credited to the address balance | Budget for occasional top-ups. Sui 1.81.1 can spend address-balance gas, but cannot request it |

| Run type | Transactions | Net cost |
|---|---|---|
| Original harness (fresh packages every run, unbatched) | 13 | 0.181 SUI |
| Fresh fixture: first run, or contracts changed | 8 | 0.176 SUI |
| Cached fixture: contracts unchanged | 3 | **0.0025 SUI** |

**Rules of thumb for new live tooling:**
- Content-address anything expensive.
- Verify cached on-chain state with free reads before trusting it.
- Invalidate on a chain-identifier or account change.
- Batch per package version.
- Clean up for rebates.
- Write each transaction's net gas into the evidence so regressions show up.

## Runs

| Date (UTC) | Base → head | Result | Spent | Notes |
|---|---|---|---|---|
| 2026-10-15 | `14b351b` → `3fe8d24` (SF-006 branch) | 7/7 PASS, 13 transactions | 0.181 SUI | base `0x3de59f64…`, upgraded `0xf5415b6e…`; first live run |
| 2026-10-15 | same | failed at upgrade | 0.109 SUI | gas budget 0.9 SUI exceeded the balance; budget lowered to 0.4 SUI |
| 2026-10-15 | SF-006 `redeploy.sh` at `8ee6c84` | dry run, publish `0xffccacbb…` (`CKP2uM91…`), upgrade `0x34f6b171…` (`J9rwj14c…`) | ~0.17 SUI | SF-006 acceptance, posted on #56 |
| 2026-10-15 | `14b351b` → `8ee6c84`+harness | fresh fixture, 7/7 PASS, 8 transactions | 0.176 SUI | base `0x30230efc…`, head `0x5a9686ae…` |
| 2026-10-15 | same | **cached fixture**, 7/7 PASS, 3 transactions | **0.0025 SUI** | fixture reuse and batching |
| 2026-10-15 | `14b351b` → SF-010 `cad0c3d` | upgrade-security 5/5 PASS, 17 transactions (5 expected aborts) | 0.221 SUI | base `0xacbd71c2…`, head `0xea8ed1de…`; real Walrus |
| 2026-10-15 | SF-010 merged with main `6e58163` | package-upgrade, cached, real Walrus, 8/8 PASS, 3 transactions | 0.0025 SUI | fixture `bde66f05…` |
| 2026-10-15 | SF-010 `f10612c` | wallet gate 10 checks PASS | 0.018 SUI | on-chain abort `2pNXPmXR…` read back as failure |
| 2026-10-15 | `14b351b` → PR #70 feedback tree | upgrade-security, fresh pair, 5/5 PASS, 17 transactions (5 expected aborts, gas charged) | 0.221 SUI (allowance 0.257) | base `0x04b78bba…`, head `0x74640ff7…`; every mutation through `Budget` |
| 2026-10-15 | same | upgrade-security, **cached pair**, 6/6 PASS, 15 transactions | 0.070 SUI (allowance 0.106) | no publish; UpgradeCap re-verified at version 2 |
| 2026-10-15 | same | wallet gate 10 checks PASS | 0.019 SUI (allowance 0.102, funding charged) | on-chain abort `N4CwDxCK…` read back as failure |
| 2026-10-15 | `14b351b` → SF-010 `ba71f90` (main `0a7a184` merged) | upgrade-security, cached pair, 6/6 PASS, 17 transactions (5 expected aborts) | 0.029 SUI (allowance 0.105) | cleanup: deployments deleted, group deleted and device deregistered (`DrTSQGcm…`); 0.033 SUI swept back from the device key (`7J4TvuhA…`); no pending key left |
| 2026-10-16 | `3ff488c` → `b81ff39` (SF-056) | fresh fixture on protocol 139, 10/10 PASS, 9 transactions | 0.411 SUI | split toolchain; base `0x0644ce0d…` (`FqzLbH84…`), upgrade `0x2976ad86…` (`HLp3bSDE…`); one faucet request |
| 2026-10-16 | same | cached fixture, 8/8 PASS, 3 transactions | 0.0020 SUI | reused the compiler-keyed fixture |


## SF-043 implementation verification

The implementation must merge through independent review before GitHub can source the new controller from main and release the main-only environment secret. Machine rehearsals establish runner behavior; they do not establish that automatic Actions execution is active. Record the first actual automatic run URL and result after integration.

On 2026-10-15, the SF-043 agent-wallet rehearsal published base `14b351b9389c058117297343ced34f262db6f3c3`, upgraded to current SF-008 contracts, activated custody, then caught a poller incorrectly rejecting pending version-1 receipts. The failed run retained evidence and bounded cleanup succeeded. After repair, exact head `08d31decbadba65ba8f6cee1a4a3a4abcb36d8e5` reused the same fixture and passed direct/group/old-package application, version-2 receipts, health-failure reporting and cleanup: **2,501,136 MIST net**, 77,452,052 MIST conservative allowance, three operator transactions. A fresh-run 1-MIST negative check refused publication before submission, produced zero transactions and spent zero. These are real chain results, using a local Walrus stand-in and host component updater; wallet/Pi/public-Walrus acceptance remains #11/#32.

The CI runtime was also exercised against the real dedicated CI wallet with a pipe-fed anonymous-memory keystore. Base `cc30b55e3f070e0ff70c2740e220ec8497adde6e` → fresh head `f373e1f9073ad20b0e36ccfd3f232ba28d5b5d19` passed all receipt/health/cleanup assertions (222,514,160 MIST net). An encrypted-cache reuse run at `0ce0152513aebe018606e77e10351d9ff15afa0f` passed in three operator transactions (2,501,136 MIST net), without republishing. A single checksum-verified 1.82.1 proof-of-work faucet request funded the CI address: `82hGEAm5NQi2SaBW1sTLwvdxUUoBMkf8CUC9JBT4m6AE`, successful execution effects independently read at checkpoint `395071723`. Its rehearsal CLI returned 1 after credit because the mounted config was read-only; the automatic runner checks resulting balance and uses a writable public runtime config, without retrying a credited request.

Exact transaction/type-origin/budget evidence is in [the SF-043 receipt](validation/sf043-testnet.json). Inspection of actual decrypted fixture state confirmed neither the device key nor the CI secret appears in public evidence or raw cached ciphertext; encrypted cache mode was 0600. Final offline suites: 92 Python script tests, 59 coordinator tests, authenticated Node cache checks under Node 22.22.2, actionlint 1.7.7, and the full pinned `make check` passed. Host-only orchestration used Node 24.21.0; CI/runtime rehearsals used the pinned container toolchain.

Final cached head `e98a24e8167e` independently verified all four receipt-writing transaction digests and successful execution effects using the pinned frontend gRPC SDK; their checkpoints are retained in the receipt. Its operator transactions again cost 2,501,136 MIST, within 72,147,588 MIST conservative allowance. Funding-tool download is deferred until a low balance actually needs a request, keeping ordinary cached runs independent of a large release download.

`queue: max` preserves unrelated pending testnet runs instead of GitHub's default single-pending replacement. This uses [documented GitHub queueing](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency); released actionlint 1.7.12 still rejects the new queue syntax. Queue-aware validation uses the inspected upstream actionlint PR #654 at exact commit `644076a59742c2d1540ebd4686eab3c308f0e562`; no linter errors are ignored. Overflow beyond GitHub's 100-run queue limit remains a visible cancellation/missing-status blocker, never a successful test.

Linter evidence correction: the released 1.7.12 parser rejects GitHub's newly documented `queue` key, so the earlier claim that this release validates the final queue-enabled workflows was incorrect. The exact inspected upstream PR #654 build validates every repository workflow without ignored diagnostics, and `go test .` passes its core parser/linter tests. A broader `go test ./...` attempt failed in two unrelated upstream maintenance generators (archive project discovery and bad-request handling); that broader tool suite is not claimed passing. The ephemeral builder was Go 1.26.1 with two workers; no project baseline/toolchain was upgraded.


## SF-045: real workflow-token preflight repair

The first actual automatic run, [38690277887](https://github.com/tyler-suifleet/suifleet/actions/runs/38690277887), failed before any testnet transaction while its `GITHUB_TOKEN` requested the environment's deployment branch policies. The old helper hid the API response. This is an observed automatic-CI failure, not successful Actions acceptance.

GitHub [documents the branch-policy metadata as publicly readable](https://docs.github.com/en/rest/deployments/branch-policies?apiVersion=2026-03-10). The repair fetches that exact public HTTPS endpoint without forwarding a token, retains strict single-main/no-reviewer/no-admin-bypass checks, rejects incomplete lists, and reports sanitized failures. Other CI/PR facts and status mutations remain authenticated. No administrator credential is added to CI.

The ordinary same-repository CI adds a **keyless** metadata probe with the actual workflow token. It has no environment, signing key or testnet mutation and is skipped for fork PRs. Failed or unavailable public metadata remains a failing check. The branch-policy read stays credential-free (Actions installation tokens are rejected by that endpoint); because anonymous reads share the runner IP's quota, `scripts/testnet_ci.py` retries only confirmed rate-limit 403/429 responses (exhausted quota, `Retry-After` or a rate-limit message) and transient 5xx/transport failures, honouring `Retry-After`/`X-RateLimit-Reset`, for at most six fresh attempts and 150 seconds inside the 5-minute job. Any other 403/4xx fails at once, a reset beyond the budget fails without waiting, and every retried result is still checked against the exact main-only/no-reviewers/no-bypass policy; there is no cached-success fallback. For this one-time controller repair, genuine exact-head/current-base machine testnet effects can supply the existing mandatory status with truthful receipt provenance; this does not claim an automatic Actions run or bypass a failing test. Source/CI/independent review remain mandatory, and the overall request is complete only after a successful normal CI-triggered full testnet Actions run is observed following integration. See [the scoped bootstrap policy](IMPLEMENTATION_ACCEPTANCE.md).


The same follow-up also repairs [run 38692509474](https://github.com/tyler-suifleet/suifleet/actions/runs/38692509474): its workflow-run runtime token advertised only `refs/heads/main: read`, so Buildx cache export failed before tests with `failed to reserve cache`. The testnet workflow now imports the ordinary CI image cache but does not attempt cache export. Fixture persistence uses the single `encrypted-testnet-fixture` artifact containing only `fixture.bin`, still authenticated AES-GCM ciphertext, never plaintext device state or the CI key. Restoration verifies producer workflow, main ref, repository and event, rejects unexpected/symlink files, and sets local directory/file modes to 0700/0600. Artifact-name discovery is independent of skipped-run volume; unknown excessive history and API/download failures fail closed rather than republishing blindly. Upload errors are not ignored, and all actual test failures remain failing jobs/statuses.

**Fixture history growth (SF-115).** Every executed run uploads a new fixture artifact, so [run 39859678522](https://github.com/tyler-suifleet/suifleet/actions/runs/39859678522) stopped before tests once 501 accumulated beyond the old five-page scan. Restore now reads the complete name-filtered history up to 10,000 artifacts (more than 90 days of routine uploads without pruning) and selects the newest trusted snapshot by creation time. It fails closed when `total_count` changes, IDs repeat, pages come back short, the response is malformed or a lookup/download fails; a shifted listing is rescanned once. None of these failures falls back to publication. A separate `prune-fixture-history` job runs after a successful executed `validate` in the same serialized concurrency group. It checks out only main (`github.workflow_sha`), has no environment, secret or test revision, and is the only job holding `actions: write`. It keeps the 10 newest trusted snapshots and the current run's artifact, then deletes older `encrypted-testnet-fixture` artifacts from this repository's main testnet workflow, oldest first and at most 50 per run. Untrusted, expired and differently named artifacts are left alone, and a deletion failure fails the job. Ciphertext is never decrypted or rewritten.

The first SF-045 machine test at head `532ffd4a588f` on base `38beecd423ff` passed all application and independent receipt-writing effects checks. Its low-balance retry exercised automatic one-attempt faucet replenishment (return code 0, balance after credit 1,491,061,660 MIST), then spent 138,470,100 MIST net within a 187,399,364 MIST conservative allowance. Contract trees were identical, so this run deliberately skipped upgrade and tested a fresh current package. That is genuine machine evidence, not successful full Actions execution, and a later repaired head must obtain its own exact-head receipt/status before integration.
