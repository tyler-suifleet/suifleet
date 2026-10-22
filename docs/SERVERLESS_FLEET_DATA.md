# Sui/Walrus fleet data — SF-082 development snapshot

**Incomplete implementation. Do not deploy this snapshot or mark SF-082 Review.**
The current daemon still sends telemetry/logs to the HTTP indexer. This change
adds the on-chain data/policy foundation, a direct-chain health reader and a
reproducible static export workflow. It does not yet replace the complete fleet
backend. The checklist below is deliberately not an acceptance receipt.

## Dependency and package boundary

SF-082/#160 consumes SF-083/#161, PR #218's published interface v1 at exact
commit `57cd3bc1aec364d302b7192c8c19eb550c10aa23`. This branch contains that
pin through an explicit merge for development. Recovery on 2026-10-22 integrated
current main, including the actual reviewed SF-083 merge
`3e796f7c2a91e88734970b0d3956469e3a86c7ed`, without conflicts. SF-083 is Done;
the dependency blocker is resolved. All 13 fleet-data Move tests pass against
that integration. This is unit evidence, not isolated-chain compatibility or
end-to-end acceptance. Keep the consumer PR draft until the remaining software
and its normal integration/CI/review gates are complete.

`contracts/fleet_data` is a separate package depending on the core and storage
packages. It adds no functions or fields to SF-083. It consumes:

- `fleet_budget::assert_owner`, `assert_may_draw`, `account_id`,
  `account_budget_id`, `account_device_id` and `registry_id`;
- `FleetBudget`, `BudgetOwnerCap` and each device's shared `GasAccount`;
- `fleet_storage::budget_id`, `blob_record`, `blob`, and the `record_*` getters;
- the certified non-deletable blob resulting from
  `fleet_storage::register_blob` / `certify_blob`.

A producer must use registration/certification before calling the new `anchor`.
The tests exercise real pinned Walrus registration and fleet ownership;
`certify_for_testing` bypasses quorum signatures **only in Move unit tests**.
There is no claim of a real storage-node certificate or browser/daemon E2E run.
No dependency layout/signature changed. Type origins for this separate package
must be recorded separately from the core manifest's per-type origins.

## Implemented data model

An owner creates a shared `FleetData` bound to a budget, storage object, registry
and chain identifier. Only the corresponding budget owner cap can administer it.
Owner enrollment creates one shared `DeviceData` per gas account/device. An
enumerable fleet dynamic-field index maps `DeviceKey { device_id }` to its writer
ID. Writers cannot enroll themselves, replace their account, erase their history
or reset cadence. Account replacement/history migration still needs integration
work; do not discard the old writer to bypass its limits.

Device writes take an immutable fleet policy and mutate only their own writer.
Every write proves the indexed writer/device/account binding and calls SF-083's
`assert_may_draw`: revoked, suspended, closed, unenrolled, paused or foreign-budget
identities cannot write. Gas quota/funding behavior remains SF-083's authority.

`report_health` stores the exact personal-message bytes and signature directly
on Sui, replacing the previous health value; no per-snapshot Walrus blob exists.
The snapshot is bounded to 16 KiB, versus the existing telemetry stream's 64 KiB.
The producer must implement deliberate bounded summaries before this path can
be enabled. Sequence numbers increase strictly and cadence uses Sui's Clock.
The default is hourly; owners can configure 1 minute to 1 day. Signatures are
verified in the browser, not parsed/verified by Move. Authorized malformed device
claims can therefore consume their own bounded cadence slot, but must never
be shown as trusted health.

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
quotas. The daemon upload scheduler, durable failures and packing are unfinished.

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
Browser Seal encryption/decryption and daemon encryption remain unfinished.

SF-083 stores blobs until the prepaid pool's end epoch and lets only the owner
extend/release them. A device cannot delete or shorten them. Expired data must
be shown unavailable. Walrus purchases are consumed, not refundable; releasing
and burning an expired blob can return its Sui object-storage rebate to the
owner. This extension currently keeps anchor metadata permanently; it exposes
no pruning or device deletion API. Metadata consumes Sui storage and must be
included in the completed cost estimate. Health values are replaced, not a
permanent health history.

The [SF-054 measurements](business/FLEET_COST_MODEL.md) and [SF-083 budget](FLEET_BUDGET.md)
are prior evidence, not a new quote for this implementation. SF-082 still needs
a real chosen-cadence quote, measured upload and per-device monthly costs,
quilt/packing evaluation, encoding/metadata overhead verification and budget
estimator integration. Do not infer a measured bill from the cadence defaults.

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
current registry revocation, the fleet key epoch and the current **Walrus** epoch
before and after a download/decryption. It verifies SHA-256 of ciphertext before
requesting keys, requires the fleet-data original Seal namespace, fleet ID,
key epoch and exact AAD, and verifies the personal-message signature against the
signer recorded by the authorized anchor transaction. Key rotation therefore
does not discard authenticated history, while current device revocation refuses
reads. The Move owner/support policy remains the confidentiality boundary.
Streaming responses have a 4 MiB limit and a 60-second deadline; archives contain
at most 4096 records, with SF-020 line/source bounds. Returned plaintext buffers
are wiped after parsing. This does not erase parsed JavaScript strings.

`archiveDecryptor` builds the owner/support approval transaction kind for Seal
without submitting it, uses a signed unexpired namespace-bound session, verifies
provider public keys and requires the configured provider set and threshold.
Its full provider/session flow is not yet covered by an end-to-end test.

The proposed archive plaintext is the exact JSON envelope `{message, signature}`;
`message` is base64 of signed schema-1 JSON with `kind: "fleet-data"`,
`chain_identifier`, `registry`, `fleet_id`, `device_id`, `signer`, `seq`,
`observed_ms`, `reason` (`logs`, `diagnostics`, `crash`), `dropped_lines` and
`records` (`source`, `line`). It is **not yet produced by the daemon or allowed
by its signing service**. AAD is UTF-8
`suifleet/fleet-data/v1/<chain>/<fleet-id>/<seq>/<kind-number>`; the Seal identity
uses the existing fleet-ID/epoch/nonce layout. This changes no SF-083 API/layout.

Offline tests use real device signatures, actual Seal SDK encryption and
independent AES-GCM decryption with test-only DEM keys; public key objects and
chain responses are fixtures. They cover signature/binding/replay/tampering,
stream/record bounds, pre-key policy rejection, expiry/revocation/key rotation
and revocation during download. They do not establish provider availability,
actual chain anchoring, storage certification or a daemon/browser round trip.

The log/diagnostic UI and browser alert derivation remain unimplemented. The
existing alert/log UI still needs the HTTP indexer. No serverless-equivalence
claim is made.

## Static Walrus Site build and future publication

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
and routing digest. The fixture-only validation used
`manifests/fixture.localnet.json`; that does not bind an actual deployment.

Walrus [Sites configuration](https://docs.wal.app/docs/sites/configuration/site-configuration)
is local metadata: `ws-resources.json` is not itself served. Preserve it after
publication because its `object_id` selects updates. A wildcard HTML route would
break Next route payloads/JS/WASM, so this workflow uses exact routes.

Publication is still unrun. Before a Testnet rehearsal, acquire/assert the shared
wallet lock, verify chain/account/site ownership and network-specific builder
configuration, enforce the run spend ceiling, and record the deployment's actual
effects, costs and resource map. The [upstream builder](https://docs.wal.app/docs/sites/getting-started/using-the-site-builder)
uses `deploy --epochs N DIRECTORY` and `--object-id ID` for an existing site.
Do not run its default Mainnet context. Mainnet writes remain unauthorized.
The repository has no automated safe publication wrapper yet.

For an update, retain the previous exported bytes, resource/routing digest
receipt, deployment manifest, site object ID and wallet/chain identity; build and
verify the new bundle, then update that exact site and verify every changed
resource through its Sui map and Walrus readback before changing any domain.
Rollback means redeploying the retained previous bundle to the same owned site;
it costs new transactions/storage and requires the same authorization/preflight.
A local export is not evidence that either update or rollback works on chain.

## Remaining implementation and acceptance

1. With actual SF-083/current-main integration now complete, test the new package
   on an isolated supported Sui chain, including existing objects, old-package
   attacks, authority changes, foreign accounts and storage compatibility.
2. Implement daemon RS2 encoding/prepaid registration/certification, bounded
   on-chain health summaries, safe signing-service intents and verify-before-sign,
   durable batching/retry/exhaustion/restart behavior and diagnostic/crash paths.
3. Complete fleet-policy Seal encryption and authenticated bulk production,
   connect the verified archive reader/decryptor to log/diagnostic UI, and derive
   all SF-020 alert rules from verified chain/data. Test tampered, replayed,
   foreign-chain, rotated/revoked-device and quota/funding failures end to end.
4. Measure Walrus overhead and compare batching/quilts, update budget costs and
   establish retention/expiry/readback evidence; do not use guessed numbers.
5. Rehearse authorized Testnet site publication/update/rollback, and provide an
   optional owner-run relay/indexer consuming the same data. Browser code cannot
   send push webhooks while closed; notifications require an opt-in relay whose
   output must not become a trust root. This relay is not yet implemented.
6. Run the complete relevant local/CI/security/automatic-Testnet checks and fresh
   independent review. No CI or review gate is waived by this draft snapshot.

Final physical/manual-wallet/production-provider/Mainnet acceptance remains
explicitly unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
The current implementation gaps above are software work in SF-082, not deferred
external acceptance and not new tickets.
