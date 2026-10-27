# Deployment manifests and publication

One manifest describes one deployment on one network. Both clients read it:
- the frontend reads it from `NEXT_PUBLIC_DEPLOYMENT_MANIFEST`, compiled into the static build;
- the daemon reads `deployment.json` through `[sui] manifest_path`.

Each client checks the manifest against the chain at startup and refuses a stale or mismatched one.

## Contents

| Field | Meaning |
|---|---|
| `schema_version`, `protocol_version` | Both `1`. Clients reject other values. |
| `network`, `chain_identifier` | `localnet`/`devnet`/`testnet`/`mainnet`, plus the first four bytes of the genesis digest (8 hex). testnet must be `4c78adac` and mainnet `35834a8a`. |
| `rpc_url` | Legacy/default Sui fullnode serving gRPC v2. |
| `rpc_urls` | Optional ordered daemon endpoint list (1–8 URLs when populated); empty/omitted uses `rpc_url`. Local `[sui] rpc_urls` overrides it. Every candidate must match `chain_identifier`. |
| `call_package_id`, `package_version` | The latest package. Every call goes here. |
| `upgrade_cap_id` | Must still name `call_package_id` at `package_version`. After a further upgrade, every client rejects this manifest as stale. |
| `type_origins` | For each type the clients use, the package that **introduced** it. Types keep their introduction package across upgrades, so newer types such as `RecipeDigestKey` and `DeviceAddressKey` have later origins than `DeviceRegistry`. |
| `shared_objects.registry` | The shared `DeviceRegistry` ID and its initial shared version. |
| `supported_clients` | Client version ranges, as `min` ≤ version < `max_exclusive`. |
| `walrus` | Publisher and legacy aggregator URL, plus optional ordered device `aggregator_urls`; see [public delivery](WALRUS_DELIVERY.md). |

Optional type-origin groups are all-or-none: the component catalog, the Seal policy,
the SF-083 fleet budget (`fleet_budget::*`), the SF-125 device inboxes and member
index (`fleet_inbox`, [DEVICE_INBOX.md](DEVICE_INBOX.md)) and the SF-130 fleet settings
(`fleet_settings::*`). A device template may add a `[budget]` section
([FLEET_BUDGET.md](FLEET_BUDGET.md)); its fleet's settings then apply
([FLEET_SETTINGS.md](FLEET_SETTINGS.md)).

`manifests/fixture.localnet.json` is a test fixture, not a deployment.

## Publishing: `scripts/redeploy.sh` (`make publish-contracts`)

`scripts/redeploy.sh` publishes or upgrades on the **active Sui CLI environment** and spends gas. With `--dry-run` it only simulates. It builds the gRPC chain reader (`suifleet-edge/examples/chain_facts.rs`), then runs `scripts/publish.py`.

```bash
# In the dev container (make shell), with the CLI on the intended environment and account:
scripts/redeploy.sh --expect-address 0xYOUR_ADDRESS --dry-run
scripts/redeploy.sh --expect-address 0xYOUR_ADDRESS
# or: make publish-contracts PUBLISH_ARGS="--expect-address 0xYOUR_ADDRESS --dry-run"
```

### Before any transaction, it stops unless all of these hold

All chain reads go over gRPC.
- **Account:** the active account is `--expect-address`.
- **Chain identifier** (the live one) matches:
  - `contracts/Move.toml`'s `[environments]` entry for this environment. testnet and mainnet are built in; a devnet or local chain must be declared there with its current identifier.
  - the pinned network label: testnet `4c78adac`; mainnet is refused outright, because this is not a production pipeline.
  - this environment's `[published.ENV]` section of `contracts/Published.toml`. Only that section is read; another environment's IDs are never used. A mismatch means the network reset.
- **Upgrade only:**
  - the `published-at` package exists at the recorded version;
  - the `UpgradeCap` names that package and version and is owned by the active account;
  - the recorded deployment (`deployments/ENV/current`) agrees with `Published.toml`.
- **Refusals:**
  - A recorded deployment without a `Published.toml` section is refused, because a fresh publish would orphan its fleet.
  - An unrecorded upgrade needs `--registry ID` to adopt the existing registry.
- **Release plan** (`--plan PLAN.json`, SF-154): the reviewed plan from `scripts/release_plan.py` must describe this exact transaction: network, chain, account, action, deployment, contract sources and gas budget. It must also be intact, ready and unexpired. Testnet publication requires one; see [RELEASE_PLANNING.md](RELEASE_PLANNING.md).

### Transaction and failure handling

- A failed CLI command, unparseable output, or execution effects other than `success` stop the run. Nothing is written.
- The publish must create:
  - exactly one package;
  - one `UpgradeCap` owned by the account;
  - one shared `DeviceRegistry` of the new package.
- An upgrade must leave the same `UpgradeCap` at version + 1.

### Recording a release

1. **Pending record.** As soon as a transaction executes, it is saved as `deployments/ENV/pending-DIGEST.json`.
2. **Verify.** `Published.toml` must now record it. The manifest is then resolved from the chain and verified (type origin table, `UpgradeCap`, registry).
3. **Write the release.** It is written to `deployments/ENV/releases/vN-PACKAGE/`:
   - `deployment.json`
   - `frontend.env`
   - the device `config.toml` template (`[sui]` plus the device sections of `suifleet-edge/config.example.toml`, or `--device-template`)
   - `transaction.json` (digest, effects status and gas used) and, when one admitted it, `release-plan.json`
4. **Switch both clients together.** The new `dapp/.env.local` is written in full beside the old one first: it gets `NEXT_PUBLIC_DEPLOYMENT_MANIFEST`, the legacy `NEXT_PUBLIC_*_ID`/`NETWORK` lines are dropped, and every other line is kept. Next, a synced journal `deployments/ENV/activating.json` names the release. Then two renames activate `deployments/ENV/current` and `.env.local`, and the journal is removed.
   - If the second rename raises an error, the first is undone, and the undo is synced before the journal is removed.
   - If both renames succeed but syncing them fails, nothing is undone and the journal stays, so the switch is finished as below.
   - If the process is killed between them, the journal stays. The next run of `scripts/redeploy.sh` finishes the switch first, rolling forward to the journaled release; the release was complete and verified before the journal was written. `scripts/redeploy.sh --reconcile` does only that and sends no transaction.
   - If finishing a journaled switch fails at any step, the journal stays, so the next run tries again.
   - While a journal exists, `--dry-run` and `make dapp-build` refuse, so a half-switched `.env.local` is never built.
5. **Recover.** If any step after execution fails, the previous `current` and `.env.local` stay in place. The error checks this and says so, or warns if either changed. It names the pending file. Fix the cause, then run `scripts/redeploy.sh --recover deployments/ENV/pending-DIGEST.json`. It finishes recording without another transaction.

Earlier releases stay under `releases/`. Once superseded, every client rejects them as stale.

Commit `contracts/Published.toml` and `deployments/ENV/` together after a public-network release: they are the record of every package, UpgradeCap, registry and type origin. Then rebuild the frontend, because `NEXT_PUBLIC_*` values are compiled in. Install `deployment.json` with each device's own `config.toml`. The template's device IDs and key path are placeholders.

Options: `--network devnet|localnet` labels a non-public chain; `--publisher`/`--aggregator` (default: the previous release, or Walrus testnet); `--gas-budget` (default 0.5 SUI; a fresh publish costs about 0.07 SUI); `--package`, `--records`, `--frontend-env ''` (skip), `--client-config`, `--reconcile` (only finish an interrupted activation).

## Resolving and verifying by hand

`scripts/deployment_manifest.py` never spends gas:
- `resolve` builds a manifest from a package, UpgradeCap and registry.
- `verify` checks one against the chain.
- `generate` writes the release bundle.

All three read the chain through `chain_facts`. Build it first:

```bash
cargo build --locked --manifest-path suifleet-edge/Cargo.toml --example chain_facts
```

## Tests

| Command | Covers |
|---|---|
| `make test-publish` | Offline, using a fake CLI and chain. The paths covered:<br>• metadata scoped to its environment<br>• account, chain and UpgradeCap checks<br>• failed, non-zero and garbled transactions<br>• post-execution failure with prior configuration kept, then `--recover`<br>• a process killed during activation, finished by `--reconcile` or the next run<br>• dry run, adoption and the strict TOML subset |
| `make test-publish-localnet` | Real localnet, real contracts and CLI:<br>• wrong account refused<br>• dry run writes nothing<br>• fresh publish and upgrade, each verified by the daemon (`--check-config`) and the frontend SDK<br>• the superseded release rejected as stale<br>• incompatible upgrade leaves everything unchanged<br>• reset-chain metadata refused |
| `make test-manifest`, `make test-manifest-live` | Manifest validation, and origins across real upgrades |

A testnet publish and upgrade has not been run by these tests. It spends real gas, needs the release account, and must be run explicitly by the operator. Record the transaction digests and the resulting manifest in the release issue.

The optional all-or-none `receipt_shards` type group enables SF-127. Its directory,
root, receipt/index keys and relocated group/catalog keys retain their own
introduction origins. Resolved manifests containing it require dApp >=0.1.1 and
edge >=0.2.2 within their existing major compatibility ranges. Existing manifests
without the group retain v2 behavior; see [sharded receipts](SHARDED_RECEIPTS.md).

The optional `group_batch` type group (`customer::GroupBatchApplied`, SF-171)
tells clients the package has the bounded batch membership entry points. Like
`fleet_handover`, it is a single-type group recorded with its own introduction
origin; manifests without it keep the single-device calls. See
[bulk group membership](GROUP_BATCH.md).
