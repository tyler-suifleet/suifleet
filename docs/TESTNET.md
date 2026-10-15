# Testnet accounts and the live package-upgrade gate

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
- **Spend.** Live runs check the chain identity and balance first, and stop at a spend ceiling (`--max-spend-mist`, default 0.6 SUI). See Cost below: a typical run costs about 0.0025 SUI.
- **Record each run.** Write the transaction digests and the evidence JSON into the PR or issue. Never record keys.

## Funding

Testnet SUI comes from the faucet's proof-of-work endpoint, which needs **Sui CLI ≥ 1.82.1**:

```bash
sui client faucet --address 0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965
# "Request successful. The faucet sent 1 SUI to the address balance of …"
```

**Older CLIs don't work.** The pinned 1.81.1 CLI only redirects to the web faucet, and `POST /v2/gas` answers "CI compatibility is disabled".

**Use 1.82.1 for funding only.** A checksum-verified 1.82.1 release binary (from the MystenLabs/sui release `testnet-v1.82.1`, sha256 `112bf204…2858c` for `ubuntu-x86_64`) is fine for requesting funds. Builds and tests stay on the pinned CLI. The faucet credits the *address balance*, which the 1.81.1 CLI can spend as gas.

**The web faucet** is the alternative: `https://faucet.sui.io/?address=ADDRESS`.

## The `package-upgrade` live gate

```bash
make live-gate-package-upgrade SUIFLEET_WALLET=$HOME/.config/suifleet/testnet/agent/client.yaml [BASE_REF=origin/main]
```

`scripts/live_package_upgrade.py`:
1. Publishes the **base** contracts (default: the merge base with `origin/main`) as a fresh ephemeral publication.
2. Creates a device (key provisioned by the daemon), its cap and a group with that package, then activates it into registry custody through the head package.
3. Upgrades in place to the checked-out tree with the run's own UpgradeCap.
4. Verifies the manifest over gRPC.
5. Runs the real daemon with the SF-030 test updater and a local Walrus stand-in, and deploys:
   - through the new package, to the device and to the pre-upgrade group;
   - through the **old** package's entry point (mixed versions);
   - an injected health failure.
6. Checks every receipt on chain with the Sui CLI, independently of the daemon, requiring current authority version 2.
7. Deletes the deployments for the storage rebate.
8. Writes evidence: tool versions, chain, packages, type origins, each transaction digest and status, receipts and SUI spent.

**Automatic CI.** `.github/workflows/testnet.yml` is sourced from main and runs after successful `CI` completion, daily at 08:23 UTC, and through main-only dispatch. The controller re-fetches originating workflow facts, verifies CI identity/success, checks PR author and run actor write authority, rejects forks, and binds the exact current PR SHA and current main base. Superseded runs are skipped before spending. Main push/periodic checks compare main with its first parent. Public fork CI remains unprivileged/offline. No arbitrary dispatch ref receives secrets.

`live-package-upgrade` is configured main-only, with no required reviewers and `can_admins_bypass=false`; this is the user's explicit standing testnet authorization. The planner verifies these settings; the runtime derives the address from the protected secret and requires the dedicated CI address. All automatic/manual testnet paths share concurrency `suifleet-testnet-ci-wallet`, `cancel-in-progress=false`, `queue: max` (up to 100 pending runs; queue overflow fails closed through missing required status). The old manual package gate delegates to this controller; wallet/hardware approvals remain unchanged.

The controller sets `testnet/package-upgrade` on the tested SHA, with `base:<exact base SHA>` in its description. A failure remains a failing job and status. The sole orchestrator requires this success for the current head/base once `testnet.yml` exists on main; the first reviewed workflow merge bootstraps before activation. Install the reviewed coordinator after merge. Missing status blocks later integration even before GitHub queues the privileged run. This testnet gate does not reinstate unrelated deferred integration blockers.

**Spending.** Default allowance is 600,000,000 MIST. Every publish, upgrade, PTB, funding transfer and cleanup uses one executor. It caps each transaction gas budget at remaining allowance, counts nonnegative gas on both successful and failed effects, reserves the full budget on unknown execution outcomes, and counts all available device gas plus new funding. Refunds/faucet credits never increase allowance. Abort cleanup uses only what remains; evidence survives failures. This is a conservative ceiling, so accounted allowance may exceed net wallet spend.

**Automatic funding.** If the dedicated wallet holds less than the run allowance, the runner may make one testnet faucet request, bounded to 180 seconds. CI downloads a funding-only Sui 1.82.1 binary with SHA-256 verified against the pinned release digest; build/test tools remain Sui 1.81.1. Faucet outages and insufficient balance fail clearly. No user hand-funding step is part of routine CI.

**Cost.** Package storage is never refunded, so the published fixture is cached. The fixture is the base package, its upgrade, the device with its key, the cap and the group.
- **Where the cache lives.** Locally it's `live-state.json` beside the wallet; on CI only AES-256-GCM ciphertext is stored in the Actions cache. The domain-separated encryption key is derived through HKDF-SHA256 from the protected CI secret; the CI key itself is never cached. Decrypted device state and public wallet configuration exist only in a mode-0700 temporary directory, files mode 0600, removed on exit. CI signing bytes travel through an in-process pipe to a Linux anonymous memfd; a private runtime symlink permits the pinned CLI to read it without a disk key file or secret command arguments. Tampering and wrong keys fail closed. It's keyed by content hashes of the base and head contracts.
- **Re-verified before reuse.** Free reads check that every object still exists with the expected owner.
- **Fresh fixture** (first run, or after any contract change): about 0.18 SUI.
- **Cached run:** about 0.0025 SUI. It sends three transactions: head-package creates, a base-package create (one transaction cannot call two versions of a package), and one delete batch that recovers the storage.
- **Identical contracts.** When base and head contracts are identical, the upgrade is skipped.
- **Device gas.** The device's report gas, about 0.004 SUI per run, is topped up with 0.03 SUI only when below 0.01. So 1 SUI covers hundreds of cached runs.
- **Forcing a new fixture.** Pass `--fresh`. A testnet reset or a different wallet discards the cache automatically.
- **Device key.** This is a signing credential: disclosure permits receipt spoofing/griefing. Never publish plaintext state in caches, artifacts or logs.

**Limits:**
- Walrus is a local stand-in, not real Walrus storage.
- There is no browser-extension wallet: that is the `wallet` gate, still unimplemented (SF-010).
- There is no hardware: that is the `hardware` gate (SF-031).
- The device keeps its unused report gas, about 0.03 SUI per run.
- **No JSON-RPC.** Public testnet fullnodes no longer serve JSON-RPC (gRPC and GraphQL only). Live tooling must read over gRPC (`chain_facts`, the daemon, the frontend SDK) or through the Sui CLI. The localnet tests' JSON-RPC helper does not work there.

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


## SF-043 implementation verification

The implementation must merge through independent review before GitHub can source the new controller from main and release the main-only environment secret. Machine rehearsals establish runner behavior; they do not establish that automatic Actions execution is active. Record the first actual automatic run URL and result after integration.

On 2026-10-15, the SF-043 agent-wallet rehearsal published base `14b351b9389c058117297343ced34f262db6f3c3`, upgraded to current SF-008 contracts, activated custody, then caught a poller incorrectly rejecting pending version-1 receipts. The failed run retained evidence and bounded cleanup succeeded. After repair, exact head `08d31decbadba65ba8f6cee1a4a3a4abcb36d8e5` reused the same fixture and passed direct/group/old-package application, version-2 receipts, health-failure reporting and cleanup: **2,501,136 MIST net**, 77,452,052 MIST conservative allowance, three operator transactions. A fresh-run 1-MIST negative check refused publication before submission, produced zero transactions and spent zero. These are real chain results, using a local Walrus stand-in and host component updater; wallet/Pi/public-Walrus acceptance remains #11/#32.

The CI runtime was also exercised against the real dedicated CI wallet with a pipe-fed anonymous-memory keystore. Base `cc30b55e3f070e0ff70c2740e220ec8497adde6e` → fresh head `f373e1f9073ad20b0e36ccfd3f232ba28d5b5d19` passed all receipt/health/cleanup assertions (222,514,160 MIST net). An encrypted-cache reuse run at `0ce0152513aebe018606e77e10351d9ff15afa0f` passed in three operator transactions (2,501,136 MIST net), without republishing. A single checksum-verified 1.82.1 proof-of-work faucet request funded the CI address: `82hGEAm5NQi2SaBW1sTLwvdxUUoBMkf8CUC9JBT4m6AE`, successful execution effects independently read at checkpoint `395071723`. Its rehearsal CLI returned 1 after credit because the mounted config was read-only; the automatic runner checks resulting balance and uses a writable public runtime config, without retrying a credited request.

Exact transaction/type-origin/budget evidence is in [the SF-043 receipt](validation/sf043-testnet.json). Inspection of actual decrypted fixture state confirmed neither the device key nor the CI secret appears in public evidence or raw cached ciphertext; encrypted cache mode was 0600. Final offline suites: 92 Python script tests, 59 coordinator tests, authenticated Node cache checks under Node 22.22.2, actionlint 1.7.7, and the full pinned `make check` passed. Host-only orchestration used Node 24.21.0; CI/runtime rehearsals used the pinned container toolchain.

Final cached head `e98a24e8167e` independently verified all four receipt-writing transaction digests and successful execution effects using the pinned frontend gRPC SDK; their checkpoints are retained in the receipt. Its operator transactions again cost 2,501,136 MIST, within 72,147,588 MIST conservative allowance. Funding-tool download is deferred until a low balance actually needs a request, keeping ordinary cached runs independent of a large release download.

`queue: max` preserves unrelated pending testnet runs instead of GitHub's default single-pending replacement. This uses [documented GitHub queueing](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency); actionlint 1.7.12 (SHA-256-verified release) validates the queue syntax. Overflow beyond GitHub's 100-run queue limit remains a visible cancellation/missing-status blocker, never a successful test.
