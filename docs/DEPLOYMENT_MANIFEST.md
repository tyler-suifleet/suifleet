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
| `rpc_url` | Sui fullnode serving gRPC v2. |
| `call_package_id`, `package_version` | The latest package. Every call goes here. |
| `upgrade_cap_id` | Must still name `call_package_id` at `package_version`. After a further upgrade, every client rejects this manifest as stale. |
| `type_origins` | For each type the clients use, the package that **introduced** it. Types keep their introduction package across upgrades, so newer types such as `RecipeDigestKey` and `DeviceAddressKey` have later origins than `DeviceRegistry`. |
| `shared_objects.registry` | The shared `DeviceRegistry` ID and its initial shared version. |
| `supported_clients` | Client version ranges, as `min` ≤ version < `max_exclusive`. |
| `walrus` | Publisher and aggregator URLs. |

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
4. **Switch both clients together.** The new `dapp/.env.local` is written in full beside the old one first: it gets `NEXT_PUBLIC_DEPLOYMENT_MANIFEST`, the legacy `NEXT_PUBLIC_*_ID`/`NETWORK` lines are dropped, and every other line is kept. Then two renames activate `deployments/ENV/current` and `.env.local`. If the second rename fails, the first is undone, so both clients always name the same release.
5. **Recover.** If any step after execution fails, the previous `current` and `.env.local` stay in place. The error checks this and says so, or warns if either changed. It names the pending file. Fix the cause, then run `scripts/redeploy.sh --recover deployments/ENV/pending-DIGEST.json`. It finishes recording without another transaction.

Earlier releases stay under `releases/`. Once superseded, every client rejects them as stale.

Commit `contracts/Published.toml` and `deployments/ENV/` together after a public-network release: they are the record of every package, UpgradeCap, registry and type origin. Then rebuild the frontend, because `NEXT_PUBLIC_*` values are compiled in. Install `deployment.json` with each device's own `config.toml`. The template's device IDs and key path are placeholders.

Options: `--network devnet|localnet` labels a non-public chain; `--publisher`/`--aggregator` (default: the previous release, or Walrus testnet); `--gas-budget` (default 0.5 SUI; a fresh publish costs about 0.07 SUI); `--package`, `--records`, `--frontend-env ''` (skip), `--client-config`.

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
| `make test-publish` | Offline, using a fake CLI and chain. The paths covered:<br>• metadata scoped to its environment<br>• account, chain and UpgradeCap checks<br>• failed, non-zero and garbled transactions<br>• post-execution failure with prior configuration kept, then `--recover`<br>• dry run, adoption and the strict TOML subset |
| `make test-publish-localnet` | Real localnet, real contracts and CLI:<br>• wrong account refused<br>• dry run writes nothing<br>• fresh publish and upgrade, each verified by the daemon (`--check-config`) and the frontend SDK<br>• the superseded release rejected as stale<br>• incompatible upgrade leaves everything unchanged<br>• reset-chain metadata refused |
| `make test-manifest`, `make test-manifest-live` | Manifest validation, and origins across real upgrades |

A testnet publish and upgrade has not been run by these tests. It spends real gas, needs the release account, and must be run explicitly by the operator. Record the transaction digests and the resulting manifest in the release issue.
