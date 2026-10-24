# Sui/Walrus fleet data

The daemon can publish signed health directly to Sui and encrypted, batched logs
and diagnostics to fleet-prepaid Walrus storage. The static dApp discovers and
verifies the chain index, decrypts authorized archives and derives alerts locally.
This is the product data path. SuiFleet hosts no application servers; an HTTP
indexer is only an optional customer-hosted reference integration (see
[the complete inventory](SERVERLESS_PRODUCT.md)). Implementation and validation are distinct: see the
validation section below for exact completed checks and outstanding gates.

## Dependency and package boundary

SF-082/#160 consumes SF-083/#161, PR #218's published interface v1 at exact
commit `57cd3bc1aec364d302b7192c8c19eb550c10aa23`. This branch contains that
pin through an explicit merge for development. Recovery on 2026-10-22 integrated
current main, including the actual reviewed SF-083 merge
`3e796f7c2a91e88734970b0d3956469e3a86c7ed`, without conflicts. SF-083 is Done;
the dependency blocker is resolved. The funding signatures/layouts are unchanged. Unit tests alone do not establish
isolated-chain compatibility or end-to-end acceptance. Keep the consumer PR draft until the remaining software
and its normal integration/CI/review gates are complete.

`contracts/fleet_data` is a separate package depending on the core and storage
packages. Its data model is separate from SF-083; the later additive storage
retention extension is described below. It consumes:

- `fleet_budget::assert_owner`, `assert_may_draw`, `account_id`,
  `account_budget_id`, `account_device_id` and `registry_id`;
- `FleetBudget`, `BudgetOwnerCap` and each device's shared `GasAccount`;
- `fleet_storage::budget_id`, `blob_record`, `blob`, and the `record_*` getters;
- the certified non-deletable blob resulting from
  `fleet_storage::register_blob` / `certify_blob`.

A producer must use registration/certification before calling the new `anchor`.
The tests exercise real pinned Walrus registration and fleet ownership;
`certify_for_testing` bypasses quorum signatures **only in Move unit tests**.
Real public-Testnet certification and native-daemon/browser evidence are recorded below.
No dependency layout/signature changed. Type origins for this separate package
must be recorded separately from the core manifest's per-type origins. The native
diagnostic reader resolves `DiagnosticKey` from the call package's BCS origin
table: that later type must not borrow `FleetData`'s earlier origin.

## Implemented data model

An owner creates a shared `FleetData` bound to a budget, storage object, registry
and chain identifier. Only the corresponding budget owner cap can administer it.
Owner enrollment creates one shared `DeviceData` per gas account/device. An
enumerable fleet dynamic-field index maps `DeviceKey { device_id }` to its writer
ID. Writers cannot enroll themselves, replace their account, erase their history
or reset cadence. `replace_account` is an explicit owner-capability operation for a current replacement
GasAccount; it preserves all history, sequences and cadence. Never discard the old
writer to bypass its limits.

Device writes take an immutable fleet policy and mutate only their own writer.
Every write proves the indexed writer/device/account binding and calls SF-083's
`assert_may_draw`: revoked, suspended, closed, unenrolled, paused or foreign-budget
identities cannot write. Gas quota/funding behavior remains SF-083's authority.

`report_health` stores the exact personal-message bytes and signature directly
on Sui, replacing the previous health value; no per-snapshot Walrus blob exists.
The snapshot is bounded to 16 KiB, versus the existing telemetry stream's 64 KiB.
The native producer truncates inventory to the 16 KiB summary bound while
retaining identity, synchronization and aggregate status. Sequence numbers increase strictly and cadence uses Sui's Clock.
New fleets default to daily health; owners can configure 1 minute to 1 day. Existing fleets retain their on-chain cadence until the owner changes it. Log archives are opt-in and the budget estimate defaults to zero log uploads. Signatures are
verified in the browser, not parsed/verified by Move. Authorized malformed device
claims can therefore consume their own bounded cadence slot, but must never
be shown as trusted health. Sustained service failures (including a full pub/sub
queue or disconnected MQTT bridge) degrade a snapshot after two minutes,
independent of the signed report cadence. The next scheduled report carries that
judgment; daily reporting still trades visibility latency for fewer writes.

`anchor` accepts only a certified, non-deletable blob recorded for this device
under the fleet's configured storage object. It records a ciphertext SHA-256,
blob ID/object ID, current transaction signer, sequence, kind, chain timestamp,
storage end epoch and Seal key epoch. Index entries are dynamic fields of the
device writer (`AnchorKey { index }`, zero-based, `anchor_count` upper bound).
A blob object can be indexed once. Sequences strictly increase across all kinds.

Periodic logs default to one anchor per day (configurable 1 hour to 7 days).
Diagnostics and crash anchors share a separate limit, default 5 minutes, with a
minimum of 1 minute. An attacker can lie about the emergency reason; SF-083's
upload/encoded-byte/write-fee quotas remain the final spend limit. Anchor cadence
alone does not prevent a compromised key registering unanchored blobs up to those
quotas. The daemon packs a signed batch, encrypts it natively, persists ciphertext and
resolved transaction bytes before submission, and resumes registration, storage-node
write, certification and anchoring after restart. Only an independently read matching
anchor acknowledges spool deletion. Storage exhaustion retains queued records within
the configured disk bound. Unknown/pruned transaction history requires reconciliation;
it is never a reason to purchase another blob.

## Confidentiality and retention

Seal IDs use this fleet object's ID (32 bytes), key epoch (u64 LE), and a random
16–64 byte nonce, in the new package's original Seal namespace. The Move policy
has only two approvals:

- `seal_approve_owner`: the current `BudgetOwnerCap` holder proves custody;
- `seal_approve_support`: the signed session's sender is explicitly granted.

There is no device/group read approval. Granting a device address explicitly as
a support account grants it that account's access; an owner should not do that
for write-only identities. A budget-cap transfer does not retain an implicit
read grant for the original owner. Support grants persist until explicitly
revoked. Revocation cannot recall keys/plaintext already obtained. Rotating the
key epoch refuses new key requests for all older epochs, including the owner's.
Native threshold encryption interoperates with the pinned Seal SDK. The signing
service permits only the configured fleet namespace, archive schema, object bindings,
provider threshold and bounded registration/certification/anchor intents. The local
keyless codec receives ciphertext, public IDs and storage-node traffic only.

SF-083 stores blobs until the prepaid pool's end epoch and lets only the owner
extend/release them. A device cannot delete or shorten them. Expired data must
be shown unavailable. Walrus purchases are consumed, not refundable; releasing
and burning an expired blob can return its Sui object-storage rebate to the
owner. SF-129 adds owner-only `prune_expired_anchors` and `burn_expired_blobs`:
once an archive's storage has ended, the owner deletes its anchor and burns its
blob, recovering their Sui deposits. Devices still cannot delete history. Live
archives' metadata consumes Sui storage until reclaimed and must be included in
the completed cost estimate; see [storage reclaim](STORAGE_RECLAIM.md). Health values are replaced, not a
permanent health history.

The [live Testnet quote](validation/sf082-walrus-quote.json) uses the pinned SDK
and actual RS2 encoding at 1000 shards: fixed encoding metadata is **64,032,000
bytes**. A small encrypted blob retained for three Testnet epochs costs 822,339
FROST in storage/write fees; 30 daily uploads cost 24,670,170 FROST per device per
30 days, before Sui gas and emergency batches. Packing 24 records costs the same
822,339 FROST as a 24-part quilt; 24 separate uploads cost 19,736,136 FROST. One
signed encrypted batch per period therefore avoids redundant fixed overhead without
requiring quilt range reads. Health has no Walrus upload cost. These are Testnet
quotes, not a production invoice, fiat price or paid-storage receipt.

The serverless estimator defaults to 720 health summaries and 30 periodic batches
per device per 30 days. Measured first health costs 7,255,298 MIST, recurring health
up to 4,459,123 MIST, and a complete periodic archive (registration, certification,
anchor) was observed at 27,075,739 MIST with protected retention in the small fixture.
This is not an upper bound. Migrated v3 storage uses keyed protection; localnet
registration gas stayed flat from 10 to 10,000 protected blobs (see
[SF-128 evidence](validation/SF128_RETENTION.md)). Unmigrated v2 retains the
history scan and 1,024-record ceiling. Protocol transaction limits and funding
still apply. The estimator uses the small Testnet fixture observation separately
from Walrus fees and projects capacity without a v3 slot ceiling. Match the
configured cadence and add diagnostic/crash allowance and headroom. See the
[sender effects](validation/sf082-device-effects.json). SF-083 quotas must
cover **encoded** bytes, uploads and write fees. Retention is the prepaid pool's
end epoch (three Testnet epochs in the fixture), not indefinite availability.
Owners extend before expiry under SF-083 authority; released/expired storage does
not replenish a device's spend allowance. Sui rebates belong to the fleet owner.
Production provider/fees/availability acceptance remains in #159.

### Protected retention integration

Actual SF-092 merge `2e9a7f4d1b35a9e69b6acc5da1a40ea39562e575` is integrated.
The additive `configure_device_retention` / `clear_device_retention` owner calls
use later-origin `DeviceRetentionKey` / `DeviceRetention` dynamic fields; existing
storage and budget signatures/layouts stay unchanged. With protection enabled,
device registration requires an explicit finite target and maximum renewal WAL
fee selected by the owner. It atomically prepays the new blob and its bounded
renewal resource from the separate protected reserves. Missing approval, incomplete
coverage, insufficient reserves or epoch limits abort the entire registration.
Devices cannot choose the retention target, delete history or spend owner caps.
Clearing admission preserves existing history and renewal obligations.

Configure the finite policy through a reviewed owner transaction before enabling
native device uploads on protected storage. The native producer keeps the same
registration/certification API and binds the current call package and per-type
origins explicitly. Browser reads support storage v1/v2/v3 and verify the current
record before and after decryption. Executor reward/gas reserves are additional
to the device transaction estimates; select the same finite horizon in the WAL
estimate and the retention policy. No background renewal worker is implied by
opening the dApp; see [retention operations](RETENTION_RENEWAL.md).

### Fleet-wide capacity and owner maintenance

Migrated v3 storage keeps renewal policies in dynamic fields keyed by blob ID.
Registration and removal touch keyed records and aggregate counters, with no
fleet-wide 1,024-slot ceiling or history scan. Archives paid through their full
finite target are counted separately and need no renewal policy; releases and
recipes keep renewal protection. Coverage, reserves and protocol transaction limits
still apply. Unmigrated v2 storage retains its 1,024-protected-blob ceiling,
history scan and fleet-wide expiry blocker until the owner migrates it.

`DeviceRetention.target` is one **absolute Walrus epoch**, not a rolling lifetime.
New blobs require a future target; applying their prepaid resources brings them
to that same exclusive expiry. On v3, an expired blob is handled per blob and does
not block unrelated registrations. Changing the new-archive target does not renew
existing blobs. Follow the [owner maintenance runbook](RETENTION_RENEWAL.md#archive-capacity-and-expiry-maintenance)
for migration, funding and per-blob recovery. Do not release expired records merely
to resume v3 admission: release removes browser readability and discards attached
capacity without a WAL refund. Permanent anchor metadata remains. Devices cannot
release their own history.

## Direct browser health reads

`NEXT_PUBLIC_FLEET_DATA` is an optional JSON object with exactly these fields:

```json
{
  "schema": 1,
  "chainIdentifier": "4c78adac",
  "registryId": "0x<registry>",
  "packageId": "0x<current-fleet-data-call-package>",
  "typeOrigin": "0x<fleet-data-introduction-package>",
  "fleetId": "0x<FleetData>",
  "budgetId": "0x<FleetBudget>",
  "storageId": "0x<FleetStorage>"
}
```

These are placeholders, not published IDs. Configuration must match the core
deployment manifest. The reader freshly checks the fullnode chain identifier and shared ownership, then the FleetData object's version, type
origin and all bindings; resolves the indexed writer, checks its type/version
and device/fleet binding; independently reads the current registry identity;
verifies the device personal-message signature with the existing SDK verifier;
validates health fields and requires its sequence to equal the anchored sequence.
It derives the existing health rules using chain inclusion time, the enforced
fleet health cadence and the browser clock, refusing future timestamps. The
local sampling interval does not make an hourly on-chain summary prematurely
offline; the HTTP reader retains its existing sampling-interval behavior. Revoked devices are unreported; verification
failures and chain outages are unavailable. Existing stale-refresh expiry still
applies. A configured chain path never falls back to an indexer on error.
As with existing contract reads, this trusts the selected fullnode's canonical
chain/object response; it is not a new fullnode proof verifier.

`dapp/src/lib/fleet-bulk.ts` now provides a bounded chain-index reader and
`fetchVerifiedArchive`. It checks fleet/device/type bindings, anchor sequence,
current registry revocation, the fleet key epoch and the current **Walrus** epoch and live fleet-owned storage record
before and after a download/decryption. The original anchor epoch is a historical
purchase bound; owner extensions update the live SF-083 record. Released or
shortened records are unavailable even if the anchor has not expired. It verifies SHA-256 of ciphertext before
requesting keys, requires the fleet-data original Seal namespace, fleet ID,
key epoch and exact AAD, and verifies the personal-message signature against the
signer recorded by the authorized anchor transaction. Signer rotation preserves verification of anchored signatures. Seal key-epoch
rotation retains anchor metadata but denies old-epoch decryption, including owner
reads. Current device revocation also refuses reads. The Move owner/support policy remains the confidentiality boundary.
Streaming responses have a 4 MiB limit and a 60-second deadline; archives contain
at most 4096 records, with SF-020 line/source bounds. Returned plaintext buffers
are wiped after parsing. This does not erase parsed JavaScript strings.

`archiveDecryptor` builds the owner/support approval transaction kind for Seal
without submitting it, uses a signed unexpired namespace-bound session, verifies
provider public keys and requires the configured provider set and threshold.
Live provider/session evidence, when completed, is recorded separately from
the offline encryption and signed-session fixtures.

The archive plaintext is the exact JSON envelope `{message, signature}`;
`message` is base64 of signed schema-1 JSON with `kind: "fleet-data"`,
`chain_identifier`, `registry`, `fleet_id`, `device_id`, `signer`, `seq`,
`observed_ms`, `reason` (`logs`, `diagnostics`, `crash`), `dropped_lines` and
`records` (`source`, `line`). Schema 2 (SF-084) adds a required `shadows` field: the
device's shadow documents when it archived, `{documents: [{name, version, updated_ms,
desired, reported, delta}], omitted}`, sorted by name, at most 128 documents and
512 KiB. Each document passes the shadow store's own checks, and its `delta` must be
exactly the one computed from `desired` and `reported`. A daemon with `[shadows]`
enabled writes schema 2 for every archive. A change in its shadows makes a routine
(kind 0) archive on the normal batch cadence, even without new log lines; the line
`Shadow state snapshot` keeps the records non-empty. Daemons without shadows keep
writing schema 1, byte for byte. The snapshot shares the 2 MiB plaintext budget
with the log records. The browser accepts schema 1 without the field and schema 2
with it, and nothing else. The shared cases are in
`interfaces/compat/archive-shadow-cases.json`. The daemon produces this envelope and the signing service validates its
shape and configured binding before signing. AAD is UTF-8
`suifleet/fleet-data/v1/<chain>/<fleet-id>/<seq>/<kind-number>`; the Seal identity
uses the existing fleet-ID/epoch/nonce layout. This changes no SF-083 API/layout.

Offline tests use real device signatures, actual Seal SDK encryption and
independent AES-GCM decryption with test-only DEM keys; public key objects and
chain responses are fixtures. They cover signature/binding/replay/tampering,
stream/record bounds, pre-key policy rejection, expiry/revocation/key rotation
and revocation during download. They do not establish provider availability,
actual chain anchoring, storage certification or a daemon/browser round trip.

The `/data/` page lists fleet-indexed devices and their newest 32 anchors,
requests owner/support diagnostics and opens verified archives using an explicitly
signed, five-minute Seal session. Account/network changes discard sessions and
in-flight plaintext. SF-020 health, deployment and service alert rules run in
`chain-alerts.ts`, using verified health and authoritative receipts. Failures are
unavailable; an outage never resolves an incident.

The [dashboard views](DASHBOARD_VIEWS.md) (SF-084) build per-device service,
alert drill-down, archive search and rollout progress pages on this verified path.

Sui/Walrus do not initiate push webhooks, background email or off-browser alert
polling. `scripts/fleet_alert_relay.cjs OWNER HTTPS_WEBHOOK PRIVATE_STATE_FILE`
is an optional owner-run process using the same browser verifier and rules. Supply
`NEXT_PUBLIC_DEPLOYMENT_MANIFEST` and `NEXT_PUBLIC_FLEET_DATA` as for the dApp;
`SUIFLEET_RELAY_TOKEN` optionally authenticates the destination. It stores incident
keys privately, retries failed delivery, deduplicates confirmed sends and retains
incidents during unavailable reads. A crash after delivery but before journal fsync
can redeliver a notification; consumers should deduplicate by alert key. Relay output
is never a browser trust input. No owner/server signing key is required.

## Native deployment

Enable `[fleet_data]` and `[fleet_data.archives]` in the daemon's
[example config](../suifleet-edge/config.example.toml), using owner-created objects
and explicit per-type introduction origins. Configure `[logs]` limits for archive
collection; no telemetry/log HTTP endpoint is required. Key isolation must receive
the same reviewed public object bindings in its signing policy.

`make build-walrus-codec` bundles the pinned Node 22.22.2/Sui 2.36.0/Walrus 1.2.35
SDK worker and WASM with esbuild 0.28.2. Install the complete artifact directory
beside the daemon and configure absolute Node/codec paths plus the manifest's codec
SHA256. No npm dependency installation or server is needed at runtime. The codec
hash is verified on every invocation. Storage-node writes have bounded concurrency
and per-node deadlines; omitted confirmations still face the SDK quorum/signature check. It supports the pinned Testnet Walrus system
only and refuses a foreign chain/system; another network needs a reviewed deployment
and configuration. This does not auto-detect an updater or restart the OS.

Preserve `state_dir/fleet-data`, the ordinary log/component state and signing
identity across platform-controlled restarts. Never delete a pending purchase to
work around an uncertain submission. Check `status.json` (`blocked`) and reconcile
saved digests against retained chain effects before owner recovery. Device or key-epoch
revocation deliberately leaves pending ciphertext reserved, rather than repurchasing.

## Static Walrus Site build, publication and rollback

The dApp now uses Next's static export with trailing-slash route files. A stable
build ID hashes source, dependencies and public environment configuration.
`tools/walrus-sites/toolchain.json` pins the upstream Ubuntu x86_64 distribution
`mainnet-v2.12.2`, archive and binary SHA-256, and exact version output. The
release label grants no Mainnet authority. The archive digest was compared with
the upstream [release asset](https://github.com/MystenLabs/walrus-sites/releases/tag/mainnet-v2.12.2)
and its extracted executable's digest/version checked locally.

Inside the pinned dev image, with locked frontend dependencies installed:

```bash
python3 scripts/build_walrus_site.py \
  --manifest /path/to/verified-deployment.json \
  --builder /path/to/checksum-verified/site-builder \
  --receipt /private/evidence/site-build.json
```

The helper verifies the builder, validates and supplies the manifest to the
build, exports `deployment-manifest.json`, creates exact page routes in
`ws-resources.json`, and records resource digests outside the export. It submits
no transactions. Run twice and compare the receipts' `files`, manifest digest
and routing digest. The identical receipts in `validation/sf082-site-build-{a,b}.json`
bind the actual Testnet deployment, fleet and public Seal configuration. Supply
`--fleet-data` and `--seal-settings` with those public JSON files for a serverless build.

Walrus [Sites configuration](https://docs.wal.app/docs/sites/configuration/site-configuration)
is local metadata: `ws-resources.json` is not itself served. Preserve it after
publication because its `object_id` selects updates. A wildcard HTML route would
break Next route payloads/JS/WASM, so this workflow uses exact routes.

The pinned builder's wallet still uses JSON-RPC, which public Testnet no longer
serves. `scripts/live_walrus_site.py` uses a gRPC adapter for the exact pinned Site
contract. `tools/walrus-sites/site-abi.json` binds every consumed function, layout,
ability and introduction origin; all descriptors are reverified before loading a
signing key. This does not claim the upstream CLI published the Site.

Under the serialized `network:suifleet-testnet` lease, run the wrapper with explicit
`--wallet`, `--builder`, `--walrus`, `--directory`, `--receipt`, `--runtime-dir`,
`--operation publish` and `--evidence` paths. It rechecks the dedicated chain,
operator and deployment manifest, then funds an isolated publisher under a strict
non-replenishing allowance. No shared operator key enters Node. The packed blob is
certified and non-deletable; every resource hash, byte range, header and exact route
is verified through Sui and full Walrus readback. Private keys, unsigned-intent
journals and staged releases stay in the private runtime directory, never artifacts.
Unknown effects retain the exact transaction bytes and reservation; reconcile them
before selecting another release. Never replace a pending publisher identity.

For an update, retain the previous exported bytes, resource/routing digest
receipt, deployment manifest, site object ID and wallet/chain identity; build and
verify the new bundle, then update that exact site and verify every changed
resource through its Sui map and Walrus readback before changing any domain.
Rollback means redeploying the retained previous bundle to the same owned site;
it costs new transactions/storage and requires the same authorization/preflight.
Use `--resume-evidence PREVIOUS_RECEIPT --operation update` with the new export and
receipt; use `--operation rollback` with the retained previous export/receipt.
Preserve the owned Site, publisher identity and private runtime journal. Additional
allowance requires an explicit `--add-funding-mist N` and verified transfer effects;
rebates do not increase it. Each new blob buys three Testnet epochs: extend or
republish before expiry.

The Testnet rehearsal published, updated and rolled back Site
`0xb1891eab584649b8b19c08fa16fda71351b973bfafef3ab1209874bbd79c7ab4`,
verifying all 121 resources and 27 routes at every step. The final-export update and
independent transaction costs are recorded in
[Site publication](validation/sf082-site-publication.json). This is a production
build on Testnet, not a production domain or manual wallet session. No SuiFleet
application server is required; browsers still need Sui fullnodes, Walrus
nodes/portal and configured Seal providers. Mainnet publication remains unauthorized.

## Validation and acceptance status

Historical foundation receipts (`sf082-foundation.json`, `sf082-resume.json`) are
incomplete snapshots, not current acceptance. The
[native Testnet receipt](validation/sf082-testnet-native.json) and
[browser receipt](validation/sf082-testnet-browser.json) record real signed health,
prepaid certified encrypted logs/diagnostics, restart without another purchase,
public Testnet providers with signed owner-session discovery/decryption, account
closure, withdrawal and key sweep. Complete disposable-device effects, including
failed attempts, appear in `validation/sf082-device-effects.json`. The
[isolated upgrade receipt](validation/sf082-data-upgrade.json) publishes real pinned
WAL, Walrus and fleet-storage dependencies, creates data objects through base
`18f337c`, upgrades compatibly, proves old bytes/origins survive, resolves newly
introduced diagnostic types and executes failing revoked-device attacks through
both old and upgraded entry points. Run `make test-fleet-data-upgrade-localnet` in
a disposable network-none pinned container; it uses the checksum-pinned 1.82.1
node and 1.81.1 compiler, with temporary empty wallets only. Keep #233
draft until all software, compatibility/end-to-end, current-head CI/security and
applicable automatic Testnet gates pass and the claim is handed to Review.

The native producer simulator uses the real daemon, gRPC shim, native encryption,
independent SDK threshold decryption and the same browser verifier. Its chain and
certificate responses are stand-ins; it does not prove real Walrus certification.
It covers retries, exhausted storage, restart without duplicate registration,
unknown/pruned outcomes, daily/emergency cadence, diagnostic/crash deduplication,
redaction, revocation, signed bindings and refusal of tampered archives.

Final production Seal provider configuration/authentication/fees/availability,
manual IdP/prover/extension-wallet acceptance, physical target work and Mainnet
writes remain explicitly unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). These do
not waive software or authorized automatic public-Testnet gates. Publishing a
Testnet fixture never establishes a production deployment or a hardware result.

## Opt-in bounded archive history (SF-121)

After deploying the additive data-package API, set `compact_history = true` in
`[fleet_data.archives]` configuration and set `compactHistoryOrigin` in
`NEXT_PUBLIC_FLEET_DATA` to the actual introduction origin of `CompactHeadKey`
and `CompactHead`. Both clients verify the package type-origin table. The daemon
option defaults to false; existing fleets, packages and archive formats keep
working. This change does not update an existing Pi image or deploy to hardware.

`anchor_compact` replaces one dynamic-field root per writer. The signed,
Seal-encrypted schema-3 archive includes the exact predecessor root BCS bytes;
the current root commits their SHA-256. Compare-and-swap against the previous
root prevents stale writers from severing history. Device authorization, fleet
storage ownership, certification, cadence and monotonic sequence checks remain.
The browser follows verified predecessor hashes and historical device signatures,
including across signer rotation. Missing/expired predecessors are unavailable,
never trusted from an unauthenticated discovery service.

Only the data-history index is bounded. Walrus Blob objects and fleet-storage
BlobRecords still exist per upload. Legacy `anchor` APIs remain callable, including
through old packages; legacy entries are preserved and are not automatically
pruned. This is not a global constant-storage claim. Browser traversal is bounded
to 64 linked archives per request; unlimited historical browsing/pagination is
not implemented. Periodic refresh rebuilds discovery from the chain root.

The native journal persists ciphertext and predecessor bytes across retries;
source records are acknowledged only after independently reading the matching
committed root. The signer's exact transaction verification permits SDK input
aliasing only when every typed argument matches and every input is used.

See [the controlled cost experiment](business/SF121_ARCHIVE_COSTS.md) for measured
retention/cleanup costs and the outstanding public-testnet certification check.


## Device-local encrypted Quilts (SF-121)

`[fleet_data.archives] quilt = true` opts into the existing daemon scheduler's
multi-item path. Configure the same option in the isolated signer service's
archive policy. Existing configurations default to regular blobs. No collector,
new server, fleet gateway or open browser is needed to publish. SF-139/#284 owns
component opaque-data intake/stream export; it must reuse this scheduler rather
than introduce another publisher.

The scheduler uses the chain's routine/emergency cadence as the collection
window, retaining the existing 2 MiB plaintext/4,096-record selection budget.
It splits at source boundaries or 256 records, into at most 16 ordered items;
the last item absorbs remaining selected records when the item limit is reached.
A single item stays a regular blob. Every item is independently signed and
Seal-encrypted with the same chain/registry/fleet/device/sequence/signer,
key epoch, access policy, reason and observation time. A separately signed,
encrypted schema-4 manifest commits each identifier, exact ciphertext byte length
and SHA-256. Identifiers (`item-00`, etc.) and Quilt patch IDs are locators, never
content digests. The manifest is the Quilt's `manifest` item; its ciphertext
digest is committed by the existing authenticated on-chain archive reference.
There is one registration, certification and anchor for the whole Quilt.
No Move object layout or existing type origin changes for this format.

The pinned Walrus 1.2.35 codec refuses more than 17 total transport items and
4 MiB of input **or resulting Quilt bytes**; this producer uses at most 17 total
items including the manifest, below QuiltV1's 666-item limit. Padding can make a
small Quilt much larger than its ciphertexts. Oversize formation stays pending
with an explicit error and never purchases storage. Encryption/envelope overhead
and padding mean splitting an already packed archive can increase bytes; this
feature supplies independently authenticated retrieval, not automatic gas savings.

Before purchasing, the daemon fsyncs every encrypted item, the manifest and its
journal, and rechecks the deterministic encoding. Restart reuses those bytes,
registration identity, certificate and signed transaction checkpoints; an
uncertain response never authorizes another purchase. Corrupt/missing artifacts
fail closed. Source spool entries retire only after independently reading the
required chain anchor; that anchor requires the certified fleet-owned blob.
The complete batch shares retention, renewal and deletion through the existing
fleet storage object. No per-patch expiry or cross-device pooling is promised.

One pending batch blocks formation of a second. The existing bounded log ring
continues its explicit overflow accounting; selected records also remain in the
pending encrypted artifacts until verified completion. Quilt publication retries
use persisted backoff (early failures one second, later doubling to one hour).
The saturating `attempts` counter records failed publication work, not an
exhaustion budget. Neither an overnight provider outage nor a device powered off
for more than seven days permanently suspends the batch: retries continue at the
bounded slow rate with the exact ciphertext and purchase/signing checkpoints.
Formation age remains journal metadata, not a wall-clock expiry gate. Disk/item
bounds and the single pending batch bound retained work; this trades freshness
and possible ring overflow for safe, automatic recovery without a second purchase.
Inspect `fleet-data/status.json` (`pending`, `items`, `attempts`, `retry_at_ms`,
`blocked`). Corrupt artifacts, revoked keys and unresolved signed effects still
fail closed; retrying does not bypass reconciliation or current authority.
Never delete purchase/signing checkpoints to force a fresh upload.
These are publication recovery controls, not firmware boot/reboot logic.

The reader accepts the reviewed QuiltV1/RS2/1,000-shard profile without tags.
The codec validates that profile before purchase. Index counts, lengths, column
ranges and item headers are checked against the bounded parent before any
length-based allocation; unsupported profiles fail unavailable.

The browser first checks the chain/identity/retention policy, authenticates and
decrypts the manifest, then resolves each identifier inside that same Quilt and
checks its expected size/digest, independent signature and Seal bindings.
Missing, substituted, replayed, withheld or inaccessible patches return unavailable;
the caller never gets a partially trusted archive. Authority is rechecked after
slow reads/key requests. Legacy regular blobs and schemas 1–3 remain readable;
compact history and shadow documents also work with schema 4. Device-local
batching does not reduce separate health writes or deployment receipts.

Compact history retains only its current authenticated head, not a permanent
set of every previously used blob. It rejects adjacent reuse and reuse of a blob
with a legacy marker; a device can re-anchor an older compact blob after another
head. Readers reject its mismatched ciphertext sequence binding and report the
older suffix unavailable. Device publishers must use fresh sequence-bound
ciphertexts for each head; compact mode does not promise global deduplication.
