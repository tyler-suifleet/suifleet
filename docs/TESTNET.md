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
2. Creates a device (key provisioned by the daemon), its cap and a group with that package.
3. Upgrades in place to the checked-out tree with the run's own UpgradeCap.
4. Verifies the manifest over gRPC.
5. Runs the real daemon with the SF-030 test updater and a local Walrus stand-in, and deploys:
   - through the new package, to the device and to the pre-upgrade group;
   - through the **old** package's entry point (mixed versions);
   - an injected health failure.
6. Checks every receipt on chain with the Sui CLI, independently of the daemon.
7. Deletes the deployments for the storage rebate.
8. Writes evidence: tool versions, chain, packages, type origins, each transaction digest and status, receipts and SUI spent.

**On CI**, dispatch `Live gates` with `gate=package-upgrade`, `network=testnet`, `ref` set to the PR head (e.g. `refs/pull/58/head`), and `confirm=package-upgrade`. Dispatch it from `main`: the environment releases its secret only to runs from `main`. The job imports the CI key into a temporary wallet inside the container and uploads `evidence/package-upgrade.json`.

**One-time setup** (repository admin):
1. Make the environment `live-package-upgrade` require a reviewer who is a human account with repository access. Agents share `tyler-suifleet`, so it must be a different account, with `prevent_self_review` enabled. Until a reviewer is set, the preflight job refuses every run.
2. Store the CI key:

   ```bash
   sui keytool --keystore-path ~/.config/suifleet/testnet/ci/sui.keystore export \
     --key-identity 0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac --json \
     | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["exportedPrivateKey"])' \
     | gh secret set SUIFLEET_TESTNET_KEY --env live-package-upgrade -R tyler-suifleet/suifleet
   gh variable set SUIFLEET_TESTNET_ADDRESS --env live-package-upgrade -R tyler-suifleet/suifleet \
     --body 0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac
   ```

**Cost.** Package storage is never refunded, so the published fixture is cached. The fixture is the base package, its upgrade, the device with its key, the cap and the group.
- **Where the cache lives.** Locally it's `live-state.json` beside the wallet; on CI it's an Actions cache. It's keyed by content hashes of the base and head contracts.
- **Re-verified before reuse.** Free reads check that every object still exists with the expected owner.
- **Fresh fixture** (first run, or after any contract change): about 0.18 SUI.
- **Cached run:** about 0.0025 SUI. It sends three transactions: head-package creates, a base-package create (one transaction cannot call two versions of a package), and one delete batch that recovers the storage.
- **Identical contracts.** When base and head contracts are identical, the upgrade is skipped.
- **Device gas.** The device's report gas, about 0.004 SUI per run, is topped up with 0.03 SUI only when below 0.01. So 1 SUI covers hundreds of cached runs.
- **Forcing a new fixture.** Pass `--fresh`. A testnet reset or a different wallet discards the cache automatically.
- **Device key.** The cache holds the throwaway test device's key, which can only spend that device's few hundredths of a SUI. Treat it as test data, not a credential.

**Limits:**
- Walrus is a local stand-in, not real Walrus storage.
- There is no browser-extension wallet: that is the `wallet` gate, still unimplemented (SF-010).
- There is no hardware: that is the `hardware` gate (SF-031).
- The device keeps its unused report gas, about 0.03 SUI per run.
- **No JSON-RPC.** Public testnet fullnodes no longer serve JSON-RPC (gRPC and GraphQL only). Live tooling must read over gRPC (`chain_facts`, the daemon, the frontend SDK) or through the Sui CLI. The localnet tests' JSON-RPC helper does not work there.

## Runs

| Date (UTC) | Base → head | Result | Spent | Notes |
|---|---|---|---|---|
| 2026-10-15 | `14b351b` → `3fe8d24` (SF-006 branch) | 7/7 PASS, 13 transactions | 0.181 SUI | base `0x3de59f64…`, upgraded `0xf5415b6e…`; first live run |
| 2026-10-15 | same | failed at upgrade | 0.109 SUI | gas budget 0.9 SUI exceeded the balance; budget lowered to 0.4 SUI |
| 2026-10-15 | SF-006 `redeploy.sh` at `8ee6c84` | dry run, publish `0xffccacbb…` (`CKP2uM91…`), upgrade `0x34f6b171…` (`J9rwj14c…`) | ~0.17 SUI | SF-006 acceptance, posted on #56 |
| 2026-10-15 | `14b351b` → `8ee6c84`+harness | fresh fixture, 7/7 PASS, 8 transactions | 0.176 SUI | base `0x30230efc…`, head `0x5a9686ae…` |
| 2026-10-15 | same | **cached fixture**, 7/7 PASS, 3 transactions | **0.0025 SUI** | fixture reuse and batching |
