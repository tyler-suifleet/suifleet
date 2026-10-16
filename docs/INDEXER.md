# Fleet indexer, API and CLI (SF-024)

`suifleet-indexer` keeps a queryable copy of the fleet: devices, groups, members, deployments, targets and authoritative receipts. It serves that copy over a versioned HTTP API, and its `ctl` subcommand is the operations CLI.

Without it, the dApp scans the whole event history in the browser on every view.

The indexer is a separate Rust crate with its own `Cargo.lock`. It uses the same Sui gRPC SDK as the daemon (`sui-rpc` 0.4). It never holds an operator key.

```bash
make indexer-build indexer-test          # build; unit tests
make test-indexer-localnet               # real localnet (start a node first, as for the other localnet tests)
```

## How it indexes

- **Events as hints.** Each event type is its own `ListEvents` stream with a durable cursor. The streams are `DeviceRegistered`, `DeviceDeregistered`, `DeviceIdentityChanged`, `GroupCreated`, `DeviceAddedToGroup`, `DeviceRemovedFromGroup`, `GroupDeleted`, `DeploymentCreated`, `DeploymentDeleted` and `DeviceStatusReported`. An event only names an object to re-read.
- **Authoritative state.** The stored state comes from the objects themselves, so a duplicated or reordered event cannot corrupt it:
  - the Device object, for its key, and whether it is in registry custody;
  - its registry identity (SF-008), for the trusted signer and revocation, and the `DeviceOwnerCap` it names;
  - the group object, for admin and members;
  - the DeploymentRecord, plus its target, recipe-digest and receipt fields. A component-catalog deployment (SF-011, `schema_version` 2) has no blob or digests of its own; `catalog_version` names the immutable `ComponentVersion` that holds them per platform. Legacy deployments have `catalog_version: null`.
  Legacy `StatusesKey` statuses are never used, because old package versions can forge them. Receipts written under an authority version below 2 are not used either, just as the dApp's `readReceipts` ignores them: older packages accepted those reports from revoked or rotated-out keys.
- **Operators.** An activated device sits in registry custody, so the Device object has no address owner. Its `owner` (the operator) is whoever owns the `DeviceOwnerCap` its identity names. That is the daemon's rule for whose deployments it applies. A device that was never activated is address-owned; `owner` is that address, `activated` is false, and it takes no deployments. Devices also carry `activated`, `revoked`, `signer` and `owner_cap`.
- **Device reconciliation sweep.** A Device and a `DeviceOwnerCap` both have `store`, so an unactivated device's owner, or an activated device's operator, can change through a plain transfer (or wrapping) without any fleet event, and `set_meta` edits a device silently. The indexer therefore also re-reads indexed devices, with their identities and owner caps, in ID order, `--reconcile-batch` (default 500) per pass, from a durable position, wrapping around. Tombstones are included, because a wrapped object can return. A device whose event names an object that is already absent on first read (for example wrapped in the registering transaction) is kept as a placeholder tombstone, without a change-feed entry, so the sweep finds it once it is unwrapped. `index` and `serve` startup run a full sweep first, so a restart never serves owners from before it. While running, an event-less change is reflected within one full sweep: about `devices / batch × interval` (for example 45k devices at 500 per 2 s pass is about 3 minutes). `GET /v1/status` reports `devices_reconciled_at` for the last completed sweep. Groups and deployments are shared, `key`-only objects that only the fleet modules change, and every such change emits a watched event.
- **Tombstones.** An object that disappears (deleted or wrapped) becomes a tombstone (`deleted: true`). Lists hide tombstones unless asked with `include_deleted`.
- **Atomic pages.** A page's rows, change-feed entries and stream cursor commit in one SQLite transaction. A crash at any point resumes without loss or duplication.
- **Type origins.** Origins come from the call package's own type origin table, and are checked against the manifest. Fields introduced by later upgrades are read only once the package defines them.

## Deployment

```bash
suifleet-indexer serve --manifest deployment.json --db /var/lib/suifleet-indexer/fleet.db \
  --tokens /etc/suifleet-indexer/tokens [--listen 127.0.0.1:8650] [--interval-seconds 2] \
  [--reconcile-batch 500]
```

**Startup.**
- The indexer reads the same deployment manifest as the dApp and the daemon ([DEPLOYMENT_MANIFEST.md](DEPLOYMENT_MANIFEST.md)), and verifies it against the chain first: chain identity, package version and origins, UpgradeCap, and registry.
- It catches up, including a full device sweep, before serving, so the API never answers from an empty or pre-restart index.
- It then indexes every `--interval-seconds`, and advances the device sweep by one batch. A failed stream or batch is retried without advancing its position.

**Network.**
- Run one instance per network and database.
- Bind to loopback, and put a TLS-terminating reverse proxy in front for remote clients. The service speaks plain HTTP.

**Resources.**
- The gRPC endpoint is the manifest's `rpc_url`. It must serve indexed `ListEvents` (Sui ≥ 1.81 in our tests) and retain history back to the first fleet event (see Retention).
- Writes are single-writer. API reads open their own read-only connections: WAL readers do not block the indexer.

**Limits.**
- No hosted instance exists. A systemd unit and packaging are not provided yet.
- The dApp still reads the chain directly. Switching it to the API is follow-up work. Device health is the exception: the dApp reads it from `/v1/health` and verifies each report against the chain ([TELEMETRY.md](TELEMETRY.md)).
- `--cors-origin ORIGIN` lets one browser origin, such as the dApp, call the API.

## Auth

**Token file.** `--tokens FILE` holds one `read|operate TOKEN` per line. Tokens must be at least 32 characters, are compared in constant time, and `#` starts a comment. Generate them with, for example, `openssl rand -hex 32`. Keep the file `0600`, owned by the service user.

**Scopes.** Clients send `Authorization: Bearer TOKEN`.
- `read` covers `GET`.
- `operate` also covers `POST /v1/transactions/*`.
- A missing or unknown token gets 401; a valid token with too little scope gets 403.

**Local only.** `--public-read` allows unauthenticated `GET` and still requires `operate` for `POST`. Do not combine it with a non-loopback listener.

**What `operate` can and cannot do.** An `operate` token only lets a client ask the service to *build* a transaction. Signing always happens elsewhere, with the operator's wallet or `ctl --key`. Chain authority is unchanged: group admin, device owner and verified recipes are enforced by the contracts, and pre-checked by the API. Device ownership and identity prechecks read the devices (custody, identity, owner cap) from chain for each request, not from the index, which may still be waiting for the sweep.

## API (`/v1`, JSON)

| Request | Returns |
|---|---|
| `GET /v1/status` | Chain, call package, tip and lowest retained checkpoint, last change sequence, per-stream checkpoint and lag |
| `GET /v1/devices?owner=&group=` | Devices (operator as `owner`, key address, arch, `activated`, `revoked`, `signer`, `owner_cap`, current groups) |
| `GET /v1/groups?admin=&device=` | Groups and members |
| `GET /v1/deployments?creator=&component=&group=&device=` | Deployments. `device=` matches direct targets and groups whose admin created the deployment, the same rule devices apply |
| `GET /v1/{devices,groups,deployments}/{id}` | One item. A deployment adds `resolved_devices` and authoritative `receipts`. Deployments carry `catalog_version` (see above) |
| `GET /v1/changes?after=SEQ&limit=&wait=SECONDS` | Integration feed: `{seq, kind, object_id, checkpoint, payload}` in order. Long-polls up to 30 s. Resume with `after=next` |
| `POST /v1/telemetry` | Signed device telemetry ingress (SF-019). It takes no token, because each snapshot carries the device's own signature. See [TELEMETRY.md](TELEMETRY.md) |
| `GET /v1/health`, `/v1/health/summary`, `/v1/devices/{id}/telemetry` | Device status, last seen and inventory; fleet counts; stored signed reports ([TELEMETRY.md](TELEMETRY.md)). Need `--telemetry-db` |
| `POST /v1/transactions/{add-devices,remove-devices,create-deployment}` | `{transaction, digest, sender, summary}`: base64 BCS of the sign-ready transaction, with gas and versions resolved by node simulation |

**Pagination.**
- Lists take `limit` (default 100, maximum 500) and return `{items, next}`. Pass `cursor=next` until `next` is null.
- Pagination is keyset, by object ID, so concurrent changes never duplicate or skip a stable item.
- Add `include_deleted=true` to list tombstones.

**Bulk requests.**
- Each is checked against the index before building, and rejected with 400 and a reason.
  - **Group changes:** the sender must be the group admin.
  - **Adds:** devices must be the sender's, and not already members. An activated device is borrowed from custody with its `DeviceOwnerCap` (`borrow_device`, `add_device`, `return_device` in one transaction); an unactivated one is passed directly.
  - **Deployments:** need the sender's own targets and lowercase-hex SHA-256 digests. Direct target devices must be activated and not revoked, since only those apply deployments.
- At most 200 devices per transaction.

**Change kinds:** `device.updated`, `device.deleted`, `group.updated`, `group.deleted`, `deployment.updated`, `deployment.deleted`, `receipts.updated`.

## CLI

```bash
suifleet-indexer ctl --api http://127.0.0.1:8650 --token-file ~/.suifleet/read.token devices --owner 0x...
suifleet-indexer ctl ... deployments --device 0x...             # one JSON object per line, all pages
suifleet-indexer ctl ... changes --after 120 --follow           # integration feed
suifleet-indexer ctl --token-file operate.token add-devices --sender 0xOPERATOR --group 0xGROUP 0xDEV1 0xDEV2 \
    --out unsigned.json                                          # build only; sign elsewhere
suifleet-indexer ctl --token-file operate.token add-devices --sender 0xOPERATOR --group 0xGROUP 0xDEV1 \
    --key operator.key --manifest deployment.json [--max-gas 500000000]
```

`SUIFLEET_API_TOKEN` can replace `--token-file`.

**The API is not trusted with the operator's intent.** With `--manifest`, which `--key` requires, the CLI verifies the manifest against the chain, reads the requested devices itself (custody and owner caps), rebuilds the exact calls from its own command line and checks the returned transaction against them before signing (or before writing `--out`). It refuses unless all of these hold:
- the key is the `--sender`'s key, and the transaction's sender;
- gas is paid by the sender and stays within budget;
- the bytes match the API's digest;
- the transaction has exactly the requested commands, in order. Each is a call into the manifest's package to the requested module and function, with no type arguments;
- every argument is an input holding exactly the requested value or object: the group and device objects, the registry and each custodied device's own owner cap, device IDs, component, version, blob, both digests, target lists, and the immutable shared clock. The borrowed device and its loan must be exactly the matching results of that device's `borrow_device`. There are no unused inputs.

A compromised or intercepted API can therefore not substitute another group, device, recipe or target, or add calls, under the operator's signature. `--out` without `--manifest` writes the file unverified and says so.

**Signing and executing.** The key file holds a 32-byte hex ed25519 seed. Success is read from the execution effects.

## Storage, retention and recovery

**Storage.**
- One SQLite file, in WAL mode with full sync. Back it up with `sqlite3 fleet.db ".backup copy.db"` while running.
- Losing it loses nothing on chain: re-run `index` against an archival node to rebuild.
- About 1 KB per indexed object plus the change feed. The feed is never pruned yet; plan retention per deployment.

**Network reset and other deployments.** The database is bound to one chain and one deployment: the registry and the origin of every package type. Stream cursors and rows are only valid for that deployment, so the indexer refuses to run when:
- the chain identifier differs: "the network may have reset";
- the registry differs, as for another publication on the same chain: "another publication is not an upgrade of the indexed one";
- a type that is already bound has another origin, or is missing.

A compatible upgrade keeps the registry and the existing origins, and may add types; it continues the same index. `--reset` discards the index (rows, change feed, cursors and sweep position) in the same transaction that records the new binding, then rebuilds it from the manifest's deployment. Without a mismatch, `--reset` changes nothing.

**Pruned history.** If the node no longer holds a stream's resume checkpoint, indexing stops with an explicit error rather than skipping events. Two ways out:
- Point `rpc_url` at a node that retains the history. This is preferred.
- `suifleet-indexer index ... --rebuild-from-objects` re-reads every *known* object and restarts all streams at the node's lowest retained checkpoint. Objects created during the pruned gap and never indexed stay unknown until they change again. Use this only knowingly.

**Reorgs.** Sui checkpoints are final, so there are no reorgs. The relevant hazards are resets and pruning, handled above.

**Crash safety.** A crash mid-page leaves neither rows nor cursor, and the page is re-read.

## Verified locally (2026-10-15)

- `make indexer-test`: 21 unit tests. They cover:
  - idempotent writes and the change feed;
  - a rolled-back page leaving neither rows nor cursor;
  - keyset pagination and filters;
  - adding the identity columns to a database from before SF-008;
  - receipts below authority version 2 ignored, and invalid versions rejected;
  - creator-only group resolution;
  - token scopes, including tokens padded by multiples of 256 bytes;
  - bulk validation, with live owners overriding a stale index, custodied devices borrowed with their owner cap, and inactive or revoked direct targets rejected;
  - verify-before-sign rejections:
    - same function with another group, device or digest;
    - an object replaced by its ID as pure bytes;
    - extra, missing or reordered calls;
    - wrong function, other package, wrong argument count;
    - unused inputs, non-call commands, a mutable clock;
    - sender, gas owner and budget;
    - another device's owner cap, the loan passed as the device, a whole result or an input in place of the borrowed device, and a borrow where none is needed;
  - pruned-history refusal;
  - the deployment binding: a compatible upgrade keeps the index, while another chain, another registry or a re-originated or dropped type is refused without changing it, and `--reset` discards rows, placeholders, cursors, the feed and the sweep position;
  - node JSON integers.
- `scripts/test_indexer_localnet.py` on a real Sui 1.81.1 localnet, all PASS:
  - the API matches chain state under limit-2 pagination: operators of devices in registry custody, identities, an unactivated device, members, targets, resolved devices and an identity-checked receipt (`report_verified_status_for_group`);
  - a revocation is indexed from its `DeviceIdentityChanged` event;
  - a component-catalog deployment is indexed with schema 2, its `ComponentVersion` and no recipe digest, and its deletion through `component_catalog::delete_deployment` keeps a tombstone;
  - no or unknown token gets 401, `read` cannot operate (403), and an invalid bulk request gets 400;
  - after a restart, the removal and deletion made while stopped appear once and earlier changes are not repeated;
  - an unactivated Device, and an activated device's `DeviceOwnerCap`, transferred by a plain `TransferObjects` (no fleet event) are reconciled while running. Owner filters follow them, each appears once in the change feed, and the API's prechecks reject the old owner, build for the new one, and refuse the unactivated device as a deployment target. When they are transferred back while the indexer is stopped, the new owner is served immediately after restart;
  - two Devices registered and wrapped into a `Bag` in the same transaction (their events name absent objects) are kept as placeholders; one unwrapped while running is reconciled by the sweep, and one unwrapped while stopped is served as live immediately after restart;
  - `ctl add-devices` on a custodied device writes an unsigned file, then verifies the borrow/add/return calls against its own chain reads, signs and executes (effects success), and the indexer shows the new membership. `ctl create-deployment` is verified call-by-call, signed and executed, and indexed;
  - the CLI change feed is in sequence order;
  - load: 400 concurrent paginated reads (8 clients) over about 45k rows had p50 19.2 ms and p95 82.4 ms (debug build, laptop-class host). The 20k synthetic devices are inserted as tombstones, since the sweep would tombstone objects that are not on chain;
  - a database bound to another chain, or to another publication on the same chain, is refused, and `--reset` re-indexes only the new deployment.

**Not yet done:**
- testnet, or archival nodes with real retention;
- desired state (SF-013), to be added after it merges;
- a localnet exercise of receipts written by an older package (covered by a unit test);
- dApp migration onto the API;
- packaging.
