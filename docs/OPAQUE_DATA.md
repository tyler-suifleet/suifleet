# Opaque device data (SF-139)

Components can queue arbitrary bytes for fleet-paid Walrus storage. SuiFleet
interprets only the bounded descriptor; it does not parse, redact, normalize or
re-encode payloads as JSON, text or log lines. Public on-chain metadata is **not
confidential**: do not put secrets in stream/key/content type/custom metadata.
**The exact chunk and upload sizes and unsalted SHA-256 of every plaintext
chunk are also public. Anyone can test candidate bytes offline against these
hashes. Seal scoping therefore does not hide low-entropy or known content
(sensor readings, status values, predictable text or known files).** Customers
requiring confidentiality against guessing must encrypt/randomize bytes in their
application before intake, keeping those keys outside public metadata. This API
preserves the required content SHA-256 index; it does not provide salted or keyed
content commitments. Payloads are signed and independently Seal encrypted.

## Intake and custody

Enable `quilt=true` and `opaque=true` under `[fleet_data.archives]`, with the
updated package, codec/checksum and signing-service configuration. Existing
configs default off. The existing `[logs]` collection bounds are still required
by the shared archive scheduler; no second uploader or service is introduced.

IPC operation `data_put` uses `topic` as the stream. Component ACLs must include
`data_put` in `operations` and authorize that name through their `streams` globs;
the native Unix peer UID, not the request's component name, establishes identity.
Administrators retain their existing authority. The payload is:

```json
{"descriptor":{"stream":"sensor","key":"sample","content_type":"application/octet-stream","metadata":{},"upload":"<64 hex retry ID>","chunk":0,"chunks":1,"total_size":3,"size":3,"sha256":[0]},"data_base64":"AP+A"}
```

The digest placeholder above must be replaced by all **32 SHA-256 bytes** for the
raw decoded chunk. Unknown fields, a different authorized topic, wrong digest,
invalid chunk geometry and oversize metadata are refused. A successful response
is `{sequence, state:"queued", storage_acknowledged:false}`: durable **custody**,
not a remote storage acknowledgement. Publication may remain pending while a
fleet is out of funds, quotas or storage availability. No queue overflow eviction
or offline-age expiry is permitted. `full` means retry with backpressure.

The standard-library Python SDK chunks files and returns custody receipts:

```python
from suifleet_ipc import Client
client = Client(component="sensor")
result = client.put_data("sensor", "capture", raw_bytes,
                         content_type="application/octet-stream",
                         metadata={"format": "customer-defined"},
                         upload=stable_upload_id)
```

The CLI uses the same API:

```bash
python3 sdk/python/suifleet_ipc.py --component sensor data_put \
  --topic sensor --key capture --file ./capture.bin \
  --content-type application/octet-stream --metadata '{"format":"binary"}' --upload "$UPLOAD_ID"
```

Preserve the upload ID and original bytes across an uncertain retry. Pending
chunks deduplicate by `(stream,upload,chunk)` and reject conflicting content or
metadata. `DataPutError.upload` and `.chunk` preserve retry information after a
partial SDK submission; resume with `start_chunk=error.chunk` (CLI
`--start-chunk`) and the same upload ID and original file. A completed upload may be submitted again; this API does
not promise global exactly-once deduplication after custody has retired.

Bounds: **24 KiB raw per chunk**, **64 MiB logical upload**, at most **2,731
chunks**, **4,096 pending chunks** and **64 MiB serialized chunk files**, whichever
fills first, plus a compact index bounded to 2 MiB. Base64/metadata overhead
means usable payload capacity is lower than
64 MiB. Stream names follow existing stream naming rules with a128-byte opaque limit; keys are 1–256 UTF-8
bytes, content types 1–128, custom metadata has at most 16 entries (keys1–64,
values0–256) and at most6,144 serialized JSON bytes. Files can encounter
backpressure midway through submission; retain the source and retry. Each payload
has a private immutable per-chunk file. Intake reads a compact
sequence/retry/length index and, for a retry, only its matching chunk; it validates
only new bytes and replaces the small index rather than rewriting payloads.
Index updates and directory scans still scale with pending item count (4,096
maximum). Archive loading may read the bounded spool; it does not clone it on
intake. Atomic writes need at most one additional chunk plus a temporary index;
the immutable pending Quilt adds at most4 MiB. Existing queue.json custody migrates
under the queue lock before intake; no queued bytes or sequence IDs change.
One-time migration temporarily needs space for both the legacy queue and chunk
files; normal intake does not duplicate the spool. Older daemon revisions that
only read queue.json cannot read the new index/chunk format. Drain custody before
such a downgrade; do not roll back the daemon over a nonempty migrated spool.

## Durable streams

Select names with `walrus_streams=["sensor"]` in `[fleet_data.archives]`. Declare
each operator-owned stream with `walrus_export=true`, `persistence="File"`,
`strategy_on_full="RejectNewData"`, no TTL, HTTP export or external exporter.
`discard_exported=true` may reclaim data **after** verified publication. Components
cannot create or select this destination through IPC, or acknowledge its export
checkpoint through the external-export API.

The existing store supplies exact retained message bytes. Large messages are
chunked into immutable files with one atomic custody-index commit per message.
If the complete message cannot fit, earlier custody can still publish to free
space; the source checkpoint remains unchanged. A stream message's
checkpoint advances only after its final chunk's index and anchor are read back;
FIFO chunk scheduling verifies earlier chunks first. Restart reuses queued source
mappings; a lost acknowledgement repeats verification, never a new purchase.
Missing/changed source streams or detected loss fail closed and retain custody.
To recover a pending batch after accidental stream removal/reconfiguration, restore
its original operator-owned stream definition and retained stream state, then
restart with streams enabled so verified checkpoint retirement can finish. Do not
edit pending/index journals or erase source state to force acknowledgement. The
shared archive queue remains blocked until this recovery; disabling stream export
is not a way to discard an already formed batch.
Each collection pass takes up to16 messages /384 KiB of raw bytes per stream
(the first message can exceed that byte target and is chunked). Multiple messages
share the next archive batch, subject to the16-chunk/8 KiB index limits. At most
16 small messages per archive cadence can drain when descriptors fit; alternating
log batches can halve that rate. Configure cadence and quotas to exceed input
rate; there is no unbounded-throughput promise.
The stream's own disk reservation is additional to the opaque queue bound.

## Shared publication and index

`fleet_archive::tick` reuses SF-121 `Pending`, codec `prepare/registered/write`,
registration, certification and durable transaction intents. Up to16 opaque
chunks, limited also to8 KiB of combined encoded on-chain item descriptors, share one Quilt plus the schema4 encrypted manifest's unchanged
`QuiltItem {identifier,size,sha256}` descriptors. A one-chunk Quilt plus manifest
is supported. Each patch contains the exact signed JSON `{kind:"fleet-opaque",
schema:1, chain_identifier, registry, fleet_id, device_id, signer,
batch_sequence, item}` inside the existing `{message,signature}` envelope;
`item` contains sequence, descriptor, base64 bytes and optional source checkpoint.
All integer metadata is bounded to safe reader ranges.

`anchor_items` atomically calls the existing certified, fleet-owned,
non-deletable-blob anchor path and adds immutable dynamic fields under DeviceData:
`ItemKey {batch:u64,item:u64}` → `DataItem`. The ordinal is0..15; public fields
are ordered as `anchor_index,sequence,stream,key,content_type,metadata,upload,
chunk,chunks,total_size,size,sha256,identifier,ciphertext_size,ciphertext_sha256`.
`metadata` is canonical Rust BTreeMap JSON bytes. `identifier` locates the patch;
the index's **ciphertext** digest authenticates it independently of every other
patch. Content length/digest authenticate raw plaintext. Each chunk's index binds
its batch through ItemKey and its immutable AnchorKey. Existing FleetData,
DeviceData, Anchor and legacy APIs/layouts are unchanged. Resolve ItemKey,
DataItem and ReadGrantKey from the actual introduction-package origin table;
never borrow FleetData's older origin after upgrade.

Urgent diagnostics/crashes retain log priority. If both routine queues remain
nonempty, opaque and log batches alternate; their shared cadence can therefore
halve opaque throughput. Fairness is persisted across restart.

Opaque batches use permanent anchors even if log `compact_history=true`.
The independent index requires permanent metadata; it does not inherit compact
log-history cost claims. Successful transaction effects alone never retire queue
custody. Independent matching anchor **and exact item-index BCS** readback precede
stream checkpoint advancement and queue retirement. The existing retry deadlines,
uncertain-transaction reconciliation, encoded-byte/upload/write-fee quotas and
fleet funding authority remain enforced. No automatic new purchase after an
uncertain outcome, key rotation or corruption.

## Scoped Seal sharing and retrieval (SF-140 boundary)

Opaque key identities are exactly **120 bytes**:
`fleet ID32 | key epoch u64 LE8 | device ID32 | SHA256(stream UTF-8)32 | nonce16`.
AAD is canonical UTF-8
`suifleet/opaque/v1/<chain>/<fleet>/<device>/<batch sequence>/<item sequence>`.
The package namespace remains the fleet-data original package. Every legacy
fleet-wide approval, including callable old packages, rejects identities longer
than104 bytes. New scoped approvals require exactly120 bytes and an enrolled
device, matching stream and current epoch. An old owner/support approval cannot
be combined with a new identity to bypass scope. Old archives remain explicitly
fleet-wide; no retroactive device/stream confidentiality is claimed.

Owner `set_read_grant(fleet,budget,cap,device_id,stream_hash,reader,allowed)` uses
an empty hash for a device-wide grant or SHA256(stream) for an exact stream grant.
`ReadGrantKey {device_id,stream_hash,reader}` is additive. The current budget-cap
holder can use `seal_approve_scoped_owner(id,fleet,budget,cap,device_id,stream_hash)`;
readers use `seal_approve_scoped_reader(id,fleet,device_id,stream_hash)`. Legacy
support membership grants **no** scoped read access. Grant revocation does not
recall plaintext or keys already obtained; rotating the fleet epoch denies older
keys, including owner reads. Shared batches never share payload encryption keys.

Managed customer administrators use the read-only
`seal_approve_scoped_customer(id,fleet,budget,customer,managed,device_id,stream_hash)`.
The customer credential must currently be an administrator, the managed fleet must
belong to that customer, and its budget must match the data fleet. This preserves
pre-adoption scoped identities without extracting the stored budget capability.
Use `scopedDecryptor` with `{kind:'customer',customerId,fleetId}` (the managed fleet
ID); it builds only that approval call. Read-only, revoked and rotated-away
credentials cannot use this path. Owner-cap and reader approvals remain compatible.

Do not self-grant address-based reader access as a substitute for managed admin
approval. Reader grants are independent delegated authority: they survive customer
credential revocation/rotation and must be explicitly revoked with `set_read_grant`.
Revocation cannot erase keys or plaintext already obtained.

`dapp/src/lib/opaque-data.ts` exports the SF139-owned reader boundary for SF140:
`listDataItems` (32-field bounded cursor page), `readDataItem`, `scopedDecryptor`,
`fetchDataItem`, `openDataItem`, `assembleDataItems`, `setReadGrant`, and
`DataItemBcs`. The full fetch verifies current chain/fleet/writer/device authority,
actual type origins, live storage/epoch, bounded Quilt geometry, exact ciphertext
hash, scoped identity/AAD, signed metadata and raw-byte digest. It rechecks
availability and immutable index after decryption; failure wipes returned buffers.
Parsed JavaScript strings remain subject to browser garbage collection; this is
not a guarantee of complete plaintext erasure from browser memory.
It never requires decrypting another stream or the fleet-wide batch manifest.
Use this opaque reader for generic items; `fetchVerifiedArchive` remains log-only
and intentionally refuses opaque patches. SF-140 owns their UI integration.
`openDataItem` is the lower-level cryptographic verifier; callers that use it
directly must supply a trusted current index/anchor. `assembleDataItems` accepts
only the original verified buffers returned by the reader, retains their authenticated
chain/fleet/device/index provenance, and refuses missing/duplicate/mixed uploads
or caller-supplied unverified copies or modified buffers. This prevents merging identically named
streams/uploads from different devices.

## Costs and acceptance

Publication remains **three storage lifecycle transactions per batch**
(register/certify/anchor), not per chunk, plus owner configuration/grant operations
and failure retries. A batch can have1..16 item-index entries. Chunking increases
permanent index count linearly; keys/custom metadata are public storage deposits.
No metadata pruning/rebate or indefinite fleet-scale affordability is claimed.
Budget the queue throughput against the actual configured archive cadence and
fleet quotas; small low-rate streams still pay Quilt/Walrus fixed overhead.
See [SF139 validation](validation/sf139-opaque.md) for measured index sizes,
effects/deposits and exact commands. Physical devices, actual mainnet and final
production-provider/manual-wallet acceptance remain unrun in
[#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159).
