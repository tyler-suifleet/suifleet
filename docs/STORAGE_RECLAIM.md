# Storage reclaim (SF-129)

Sui refunds most of an object's storage deposit when the object is deleted.
SF-129 lets a fleet owner delete what the fleet no longer needs: expired
archive anchors and their Walrus blobs, and finished deployment records. Every
call is owner- or creator-authorized, additive and re-checked on chain, so a
stale browser plan aborts instead of deleting live data.

WAL paid to Walrus for storage and writes is consumed and is not refunded.
Only Sui object deposits come back.

## Contract calls

### Archives (`fleet_data`)

| Call | Authority | Effect |
|---|---|---|
| `prune_expired_anchors(fleet, writer, budget, cap, storage, system, indices)` | `BudgetOwnerCap` of the fleet's budget | Removes each listed `AnchorKey` entry and its reuse marker. A blob the fleet storage still holds must have expired: its current Walrus end epoch, including any renewal, is at most the current epoch. The blob is then released through `fleet_storage::release_blob` and burned. A blob released earlier needs only its anchored end epoch to have passed. |
| `burn_expired_blobs(fleet, budget, cap, storage, system, ids)` | Same | Releases and burns expired fleet blobs that no legacy anchor indexes, such as unanchored uploads or SF-121 bounded-history archives. The anchor of a blob burned here can be pruned afterwards. |

- `walrus::blob::burn` does not check expiry. The guard in `fleet_data` is the
  only protection against early loss, and it aborts with `EUnexpired` (8).
- Each call takes at most 64 entries and is atomic: if one archive in a batch is
  unexpired, nothing in that batch is deleted.
- `anchor_count` never decreases and indices are never reused, so later
  history keeps its positions. Readers treat a missing index below
  `anchor_count` as pruned.
- A pruned blob cannot be anchored again: `anchor` requires a live fleet storage
  record.
- New events `AnchorsPruned` and `ExpiredBlobsBurned` have the upgrade's own
  type origin. No existing layout, signature or `fleet_storage` code changes;
  SF-128 owns `fleet_storage`'s protected-set layout and keeps the
  `release_blob` signature.

### Deployment records (`desired_state`)

A `Retirement` value proves, within one transaction, that no group still
desires a record. It has no abilities, so it cannot outlive that transaction.

1. `begin_retirement(record)`: creator only. A managed record lists its target
   groups for checking; an unmanaged record needs none, because only
   `set_component` (which requires a targeted group and marks the record
   managed) can make a record desired.
2. `confirm_not_desired(retirement, group)` for each target group: aborts with
   `EStillDesired` (7) while the group's current desired state references the
   record.
3. `retire(retirement, record)`: aborts with `EUnconfirmed` (8) if a group is
   still unchecked. Catalog records remove their binding through
   `component_catalog::delete_deployment` first; staged rollouts use
   `delete_rollout`. Devices already refuse deleted records.

The additive `deployment_manager::schema_version` getter selects the deletion
path. The existing creator-only `delete_deployment` calls are unchanged. A
record whose target group was deleted cannot complete the on-chain check,
because a deleted object cannot be read. The dApp lists such records as kept;
the creator can still delete them individually after review.

## dApp

- **Network budget → Reclaim expired archives.** Scans every enrolled device's
  chain index at the live Walrus epoch, lists expired indexed archives and
  unindexed expired device blobs, and offers a dry run. Owner-registered
  release and recipe blobs are never selected.
- **Deployments → Reclaim finished deployment records.** Lists the account's
  records that are cancelled, past their expiry or reported by every resolved
  target, and keeps any record a target group still desires, with the reason.
- **Dry run first.** The transaction is built with the owner's actual gas coins
  and those exact bytes are simulated. The panel shows the SUI returned after
  the transaction's own fees, with its rebate, computation and new-storage
  parts. *Sign and reclaim* is enabled only after a successful dry run, and the
  confirmed net amount is read back from executed effects.
- **Batching.** At most 64 entries per call and 256 removals per transaction.
  Larger histories are reclaimed in successive transactions.
- **Scanning.** The planner lists only the index entries that still exist, so
  pruned history costs no reads and never blocks later archives. Each device
  lists at most 1,024 fields per scan, about 512 live archives. Past that,
  *Continue scan* resumes from the device's listing cursor.
- **Readers.** `readAnchorEntries` and the public export skip pruned indices.
  `readAnchorAt` reads exactly one index, so a pruned index never resolves to
  the next archive.

Library: [`dapp/src/lib/storage-reclaim.ts`](../dapp/src/lib/storage-reclaim.ts).

## Measured costs

All figures come from an isolated network-none chain using the pinned 1.82.1
protocol, reference gas price 1000 MIST. They are not a public-network price.
Raw effects are in [sf129-localnet.json](validation/sf129-localnet.json).

The Walrus system is a one-member fixture: its epoch is set directly and its
committee key is the G1 generator. Certification uses a real BLS certificate
for that committee. Each weekly archive is one 100-byte blob with three
purchased epochs, the default retention for new archives.

| Per weekly archive | MIST |
|---|---:|
| Share of the storage purchase transaction | 136,755 |
| Device registration | 7,349,146 |
| Certification | 1,030,957 |
| Anchor | 6,388,898 |
| **Write path total** | **14,905,756** |
| Returned by reclaim, one archive alone | 12,556,305 |
| Returned by reclaim, per archive in a batch of 7 | 13,413,972 |
| **Net after reclaim, batched** | **1,491,784** |
| Net after reclaim, alone | 2,349,451 |

Net figures are signed cashflow: computation plus storage charged minus storage
rebate. Per-transaction values include rewrites of shared objects (Walrus
`System`, `FleetStorage`, writers), so the returned deposit is larger than any
single write transaction's net deposit. The whole-fixture lifecycle is the
robust total: 10 blobs (8 anchored, 2 unanchored) cost **2,512,665 MIST per blob**
from purchase to reclaim. That is less than the 3,200,000 MIST of computation
fees per blob, because reclaim also refunds deposits that owner and
shared-object writes paid earlier.

An unanchored blob returns 7,729,733 MIST. Retiring two finished deployment
records in one transaction returned 30,643,137 MIST, about 15.3M each. This is
consistent with the historical 11,751,525 MIST single deletion in the
[cost model](business/FLEET_COST_MODEL.md).

### Locked deposit per device at the default retention

Each live archive holds at least the deposit its reclaim returns, about
**13.41M MIST (0.0134 SUI)**. The table assumes weekly archives with three
purchased Walrus epochs.

| Network epoch length | Retention | Live archives per device | Locked while reclaim keeps up |
|---|---:|---:|---:|
| Mainnet, 14 days | 42 days | 6 | 80,483,832 MIST (0.080 SUI) |
| Testnet, 1 day | 3 days | 1 | 13,413,972 MIST (0.013 SUI) |

Without reclaim, the deposit grows with every archive: after one year, 52
weekly archives lock **697,526,544 MIST (0.70 SUI) per device**. Reclaim turns
that growing deposit into a fixed amount set by the retention window.

Mainnet gas prices and the Mainnet Walrus epoch schedule were not measured.
Applying these figures to Mainnet requires its reference gas price; the object
sizes and storage-unit price are protocol constants.

## Validation

| Check | Command | Result |
|---|---|---|
| Move unit tests (`fleet_data`) | `sui move test --path contracts/fleet_data --build-env testnet` | 28/28 PASS: expiry, atomic batch, released blob, owner, double prune, re-anchor refusal |
| Move unit tests (core) | `sui move test --path contracts` | 177/177 PASS: still desired, unconfirmed or untargeted group, non-creator, mismatched record, catalog and rollout paths |
| Move security gate | `python3 scripts/check_move_security.py` | PASS |
| Interfaces | `make check-interfaces` | PASS |
| dApp unit tests | `node scripts/test_storage_reclaim.cjs` | PASS |
| Affected dApp tests | `test_fleet_bulk`, `test_fleet_recovery`, `test_shadow_documents`, `test_dashboard_views`, `test_fleet_data`, `test_data_session_browser` | PASS |
| Production build | `pnpm build` | PASS |
| Localnet lifecycle and upgrade | `make test-reclaim-localnet` | PASS, see below |
| Dedicated Testnet | `scripts/live_fleet_reclaim.py` | PASS, see below |

The localnet lifecycle test publishes the base core and `fleet_data`, then
creates real archives and deployment records. It upgrades both packages in
place and checks type origins and that existing objects are unchanged. It then
reclaims through the dApp planner and asserts that every dry run equals the
executed net effect. These refusals are recorded as real failed effects:

- an unexpired prune (`fleet_data::burn_expired`, 8)
- an unexpired burn (same abort)
- re-anchoring a burned blob
- retiring a still-desired record (`desired_state::confirm_not_desired`, 7)

A device that presents the owner's cap is rejected before execution.

### Dedicated Testnet run

Evidence: [sf129-testnet.json](validation/sf129-testnet.json). This was an
upgrade of the existing SF-082 fleet only: no new fleet, device writes or
mainnet.

The run verified:

- chain `4c78adac`
- the agent account
- the `fleet_data` UpgradeCap
- the fleet's `BudgetOwnerCap`

The pinned compiler's bytecode was checked in the unsigned upgrade before
signing.

| Step | Digest | Net MIST |
|---|---|---:|
| Compatible `fleet_data` upgrade `0xb96f…8673` → `0xfe96…385c` | `m1LNQsAz5MMyjNwU87yxNPdwRWzAGbhnf6B6Mvs9KhM` | 61,724,163 |
| Prune of an unexpired archive, aborted on chain `fleet_data::burn_expired` 8 | `9XEgvdYVCvAQbpZBZJo6CnBwwunEQ5tnN9v1BxPzshk1` | 1,001,546 |

After the upgrade, existing `FleetData`, `DeviceData`, `AnchorKey` and `Anchor`
types keep their base origin. `AnchorsPruned` and `ExpiredBlobsBurned` have the
new package's origin. The fleet and writer objects were byte-for-byte unchanged.

The run used a 150,000,000 MIST non-replenishing allowance and spent
62,725,709 MIST. One faucet request through the pinned transaction CLI happened
in an earlier preflight of this run, which then stopped before any write. It
was the only faucet request, and it does not replenish the allowance.

**Not yet run on Testnet:** the two SF-082 archives' storage ends at Walrus
epoch 563, and the run happened at epoch 559. No Testnet archive was expired,
so no successful Testnet prune or rebate is claimed. Once the Walrus epoch
reaches 563, rerun the same script with `--reclaim` while holding
`network:suifleet-testnet`. It reuses the cached, re-verified upgrade and
records the executed rebate.

## Limits

- Physical-device, Mainnet and production-provider acceptance remain in SF-031
  (#32) and SF-087 (#159). This work changes no device-side behaviour.
- On Testnet, the core `desired_state` change is exercised by the automatic CI
  package-upgrade and upgrade-security suites (`scripts/ci_testnet_wallet.py`),
  which run for contract changes. This manual run upgraded only `fleet_data`.
- Reclaim is owner-initiated in the dApp. No unattended worker prunes on a
  schedule.
- The retention worker does not run reclaim, and reclaim does not renew
  anything.
