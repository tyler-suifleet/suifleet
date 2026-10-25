# Sharded deployment receipts (SF-127)

Version 4 receipts use sixteen independent shared `ReceiptShard` roots. A report
borrows the deployment immutably and mutates only the selected root and its own
receipt field. The shard index is the last byte of the device ID modulo sixteen.
Every root stores fixed-size identity, index, count and migration counters. Each
receipt uses `ReceiptKey { device } -> ShardedReceipt { status, failed, reported_v3 }` and a
separate `ReceiptIndexKey { index } -> ID` field. Neither reporting nor retrying
rewrites a growing map. A retry updates the existing receipt without appending an
index; the failure bit stays sticky for rollout settlement.

The storage layout has no fleet-sized or shard-sized value and no receipt-count
cap below the functional target of about 120,000. The measured population is
1,000, **not** a 120,000-device load/concurrency test. Sixteen roots bound report
contention to sixteen independent mutation streams; reports sharing one root
still serialize. Existing target lists and group membership representations keep
their own object, input-size and linear membership-check limits. Large fleets
must target multiple groups rather than build a 120,000-ID deployment field.

## Authority and compatibility

Existing public signatures and existing struct layouts/abilities remain unchanged.
Legacy creation/report calls retain v2 behavior. The dApp opts new ordinary and
rollout deployments into v4 when the verified manifest includes the all-or-none
`receipt_shards` origin group. Existing manifests remain usable for v2. Catalog
records can opt in through `component_catalog::begin_sharded_receipts`; their
binding is relocated to `ShardedCatalogBindingKey` before the barrier is set.

Managed customer receipts retain authority version 3 and their original type
origins/layouts. Sharding uses **version 4**, distinct from managed v3. A managed
record must be opted in through `customer::begin_sharded_receipts`, which checks
current administrator and fleet binding. Its v3 snapshot freezes atomically;
previous managed-package report and migration calls reject version 4 before
writing. Managed reports use `customer::report_status_v4` or
`customer::report_group_status_v4`: customer/fleet/registry, current device
membership, deployment membership, active cap and direct/group targeting are
validated on every call. The unscoped shard report routes reject managed records.
`customer::begin_receipt_cleanup` and `customer::delete_deployment` preserve fleet
membership and catalog/rollout cleanup while permissionless bounded continuations
remain available. The daemon and signer distinguish both routes; readers accept
unsharded version 3 only with the managed binding.

The existing shard API names ending `_v3` and the `reported_v3` field are retained
for ABI/layout compatibility; they now operate only under the version 4 barrier.
The report APIs are `report_status_v3` and `report_status_for_group_v3`.
They check the current registry signer/identity, direct targeting or current group
membership/admin, status, root ID, deployment ID and deterministic device shard.
The root must be named in this deployment's directory and migration must be
complete. A root from another deployment, an unrelated shared object, a wrong
partition, revoked/rotated key or changed group authority cannot supply a receipt.

`Receipts` keeps its old fields, with `version = 4` and an empty legacy status map
after migration. Current `receipt_statuses`/`receipt_status` explicitly abort for
v4; they cannot represent independently shared objects. Use `receipt_roots`,
`receipt_status_v3` and `receipt_page_v3` (maximum 64 entries). Migration/cleanup mutation paths reject future receipt generations. Unknown receipt
versions fail closed in updated clients. `has_current_receipts` denotes the
legacy v2/unmanaged or v3/managed view only. Do not infer a v4 authoritative view from that legacy flag.

`ReceiptDirectory` is attached under `ReceiptShardKey`; its presence identifies
the shard barrier. Ordinary/rollout records use inline schema 4, while catalog
records retain inline schema 2 so every previously published handover continues
to reject them. `original_schema` preserves ordinary/catalog/rollout semantics
(1/2/3). The daemon, dApp and indexer resolve the directory and relocated fields
independently of the inline schema. The verified manifest records every new type's actual introduction origin. The resolver sets
minimum dApp/edge versions to 0.1.1/0.2.2 when the shard types are present, so
older clients cannot interpret the empty legacy map as the fleet's full result.

Old package versions stay callable. The v2 and managed-v3 receipt version checks reject v4
writes. **Early deletion APIs do not check schema version**, so migration also
relocates `TargetGroupsKey` to `ShardedTargetGroupsKey`. Their unconditional old
field removal then aborts instead of deleting the deployment and orphaning roots.
Catalog migration similarly removes the binding key understood by old catalog
deletion. Keeping inline schema 2 also rejects old ordinary deletion and old
handover throughout migration, reporting and retirement; the old publisher must
never remain an authority after a creator-only handover. Supersede catalog
records under a recipient-owned catalog instead. The final current-package
deletion restores and removes group targets in the same transaction after every root has been retired; there is no externally
visible interval in which old deletion is re-enabled with live roots.

## Resumable migration and deletion

1. The creator calls `begin_sharded_receipts`, or the catalog variant. Repeated
   starts do nothing. All sixteen roots and the barrier are committed atomically.
   Empty records are ready immediately. Otherwise the v2 snapshot is frozen;
   v4 reports/authoritative reads wait for the complete copy.
2. Call `migrate_receipt_chunk` for every root, with a limit of 1..64 old entries
   inspected per call. Each root keeps its cursor. Copies are selected by device
   shard; retries of completed roots do nothing. Pre-v2 statuses are discarded
   under the existing authority rule; genuine v2 statuses are copied.
3. `finish_receipt_migration` requires all sixteen completed roots. It clears the
   old map and exposes all roots together. Repeating finish is safe. Copying and
   clearing the old bounded v2 snapshot is operator-funded migration work, not a
   reporting-device expense; it still accesses the original v2 field.
4. Creator-only `begin_receipt_cleanup` permanently stops new reports/reads.
   `cleanup_receipt_chunk` removes at most 64 receipts and their index fields
   from one root. `retire_receipt_shard` consumes only an empty, bound root and
   records its retirement. These permissionless continuations can safely resume
   after interruption, including an abandoned partial migration.
5. The original deletion API (ordinary, catalog or rollout) consumes the parent
   only after every root is gone. It removes the directory, remaining legacy
   snapshot and ordinary extension fields. Deleting early aborts.

The dApp's deletion button performs bounded steps; use **Continue deletion**
after retirement starts. The unsigned operations CLI also supports
`migrate-receipts` and `cleanup-receipts` requests with `deployment` and `sender`.
Repeat a confirmed step until migration is ready or deletion finishes:

```json
{"command":"migrate-receipts","sender":"0xYOUR_ADDRESS","deployment":"0xDEPLOYMENT","out":"next-step.json","maxGasMist":500000000}
```

Build with `node scripts/suifleet_ops.cjs MANIFEST build REQUEST.json`; sign the
result through the existing wallet flow. This creates no new application server.
The builder copies at most 64 legacy entries per root per migration call. Cleanup
wallet transactions touch at most four roots (at most 512 deleted fields), staying
below Sui's aggregate native child-object limit. A caller composing additional
PTB commands must preserve aggregate transaction limits too.

Copied v2 receipts have `reported_v3 = false` and contribute no rollout failure
bit. Older v2 packages predating rollout admission can write ordinary receipts;
promoting a copied receipt into a future grant would bypass that admission gate.
Only a fresh admitted sharded v4 report marks rollout-settlement provenance. Existing
coordinator outcomes, including sticky failures, remain intact during migration.

V4 status reports never mutate a rollout coordinator. `sync_rollout_receipts` copies proven
shard outcomes in chunks of at most 64 devices, retaining sticky failures; the
active dApp appends this before `advance_rollout_onchain`. The gate uses device-signed
on-chain health and requires the optional rollout-health introduction origins.
The daemon resolves schema 4 through the directory before sending health. Health
requires a settled outcome, so a report awaiting settlement retains its durable
health retry; the next operator advance settles the receipt, and a subsequent
advance can consume the device health sample after that retry. Rollout progress
can therefore show an inflight grant between a report and its next settlement. A report still
requires an admitted, non-skipped rollout member. No receipt or health attestation
bypasses admission, failure thresholds, pause/abort or the existing 512-member
rollout snapshot limit. Device-signed rollout health still mutates the coordinator
and keeps the bounded health collection introduced by SF-138; the receipt storage
bound is not a claim of 120,000-member staged rollout health throughput.

## Evidence and limits

The versioned local receipts are [the 1,000-target upgrade/cost/cleanup run](validation/sf127-localnet.json)
and [old-package deletion plus real daemon/indexer interoperability](validation/sf127-clients-localnet.json).
Both ran on disposable network-none local chains. Single-report gross cost
(computation plus storage before rebates) was 10,775,200 MIST at report 71 and
10,985,200 MIST at report 1,000 (current-main integration), versus the default float of 60,000,000 MIST.
The evidence retains each measured transaction's gas, rebate and digest; batches
use at most five reports with a float-sized total budget. These are local pricing
measurements, not a public-network fee guarantee.

The client probe upgrades an actual pre-identity package, demonstrates refusal
of its unguarded deletion path, checks a real daemon's effects-checked sharded Failed
report after a missing recipe, and independently materializes that receipt with
the actual indexer. It also verifies the device-signed unhealthy sample and its
execution effects after a schema-4 rollout receipt is settled from its shard. The existing OTA suite covers successful artifact application.
Public-testnet CI is extended to populated migration, old-package rejection, a
durable Applied report retried by the real daemon through the shard route without updater replay, revocation and complete
cleanup. Its pushed-head result remains a required coordinator gate; pending CI
is not successful acceptance. Physical, mainnet and final manual/provider/wallet
acceptance remain in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159), unrun here.

Current-main integration regression: `python3 scripts/test_protocol_localnet.py
--managed-receipt-shards` runs a pre-customer → managed current-main → sharded
upgrade on a disposable network-none chain. It preserves prior type origins,
copies a populated managed snapshot, executes previous managed-package attacks
during/after migration, checks reassignment rejection and bounded deletion, and
reads the real daemon's sharded Failed report independently.

Current-main measurement (2026-10-25): the complete 1,000-target run in
`docs/validation/sf127-localnet.json` measured a maximum single receipt gross
cost of 10,985,200 MIST (1,470,000 computation + 9,515,200 storage), below the
60,000,000 MIST device float. Storage rebate was 4,378,968 MIST; later cleanup
rebates belong to the operator. `network-costs.ts` uses this measured breakdown.
The complete isolated security run charged 799,183,332 MIST of its strict
850,000,000 MIST allowance; see `sf127-upgrade-security-localnet.json`.
