# Sharded deployment receipts (SF-127)

Version 3 receipts use sixteen independent shared `ReceiptShard` roots. A report
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
rollout deployments into v3 when the verified manifest includes the all-or-none
`receipt_shards` origin group. Existing manifests remain usable for v2. Catalog
records can opt in through `component_catalog::begin_sharded_receipts`; their
binding is relocated to `ShardedCatalogBindingKey` before the barrier is set.

The new report APIs are `report_status_v3` and `report_status_for_group_v3`.
They check the current registry signer/identity, direct targeting or current group
membership/admin, status, root ID, deployment ID and deterministic device shard.
The root must be named in this deployment's directory and migration must be
complete. A root from another deployment, an unrelated shared object, a wrong
partition, revoked/rotated key or changed group authority cannot supply a receipt.

`Receipts` keeps its old fields, with `version = 3` and an empty legacy status map
after migration. Current `receipt_statuses`/`receipt_status` explicitly abort for
v3; they cannot represent independently shared objects. Use `receipt_roots`,
`receipt_status_v3` and `receipt_page_v3` (maximum 64 entries). Migration/cleanup mutation paths reject future receipt generations. Unknown receipt
versions fail closed in updated clients. `has_current_receipts` denotes the
legacy v2 view only. Do not infer a v3 authoritative view from that legacy flag.

`ReceiptDirectory` is attached under `ReceiptShardKey`. Schema 4 is an additive
barrier; `original_schema` preserves ordinary/catalog/rollout semantics (1/2/3).
The daemon, dApp and indexer resolve that boundary explicitly. The verified
manifest records every new type's actual introduction origin. The resolver sets
minimum dApp/edge versions to 0.1.1/0.2.2 when the shard types are present, so
older clients cannot interpret the empty legacy map as the fleet's full result.

Old package versions stay callable. The v2 receipt version check rejects v3
writes. **Early deletion APIs do not check schema version**, so migration also
relocates `TargetGroupsKey` to `ShardedTargetGroupsKey`. Their unconditional old
field removal then aborts instead of deleting the deployment and orphaning roots.
Catalog migration similarly removes the binding key understood by old catalog
deletion. The final current-package deletion restores and removes group targets
in the same transaction after every root has been retired; there is no externally
visible interval in which old deletion is re-enabled with live roots.

## Resumable migration and deletion

1. The creator calls `begin_sharded_receipts`, or the catalog variant. Repeated
   starts do nothing. All sixteen roots and the barrier are committed atomically.
   Empty records are ready immediately. Otherwise the v2 snapshot is frozen;
   v3 reports/authoritative reads wait for the complete copy.
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
Only a fresh admitted v3 report marks rollout-settlement provenance. Existing
coordinator outcomes, including sticky failures, remain intact during migration.

Reports never mutate a rollout coordinator. `sync_rollout_receipts` copies proven
shard outcomes in chunks of at most 64 devices, retaining sticky failures; the
active dApp appends this before `advance_rollout`. Rollout progress can therefore
show an inflight grant between a report and its next settlement. A report still
requires an admitted, non-skipped rollout member. No receipt or health attestation
bypasses admission, failure thresholds, pause/abort or the existing 512-member
rollout snapshot limit.

## Evidence and limits

The versioned local receipts are [the 1,000-target upgrade/cost/cleanup run](validation/sf127-localnet.json)
and [old-package deletion plus real daemon/indexer interoperability](validation/sf127-clients-localnet.json).
Both ran on disposable network-none local chains. Single-report gross cost
(computation plus storage before rebates) was 10,515,200 MIST at report 71 and
10,585,200 MIST at report 1,000, versus the default float of 60,000,000 MIST.
The evidence retains each measured transaction's gas, rebate and digest; batches
use at most five reports with a float-sized total budget. These are local pricing
measurements, not a public-network fee guarantee.

The client probe upgrades an actual pre-identity package, demonstrates refusal
of its unguarded deletion path, checks a real daemon's effects-checked v3 Failed
report after a missing recipe, and independently materializes that receipt with
the actual indexer. The existing OTA suite covers successful artifact application.
Public-testnet CI is extended to populated migration, old-package rejection, a
durable Applied report retried by the real daemon through v3 without updater replay, revocation and complete
cleanup. Its pushed-head result remains a required coordinator gate; pending CI
is not successful acceptance. Physical, mainnet and final manual/provider/wallet
acceptance remain in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159), unrun here.
