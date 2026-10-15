# Fleet indexer, API and CLI (SF-024)

`suifleet-indexer` keeps a queryable copy of the fleet: devices, groups, members, deployments, targets and authoritative receipts. It serves that copy over a versioned HTTP API, and its `ctl` subcommand is the operations CLI.

Without it, the dApp scans the whole event history in the browser on every view.

The indexer is a separate Rust crate with its own `Cargo.lock`. It uses the same Sui gRPC SDK as the daemon (`sui-rpc` 0.4). It never holds an operator key.

```bash
make indexer-build indexer-test          # build; unit tests
make test-indexer-localnet               # real localnet (start a node first, as for the other localnet tests)
```

## How it indexes

- **Events as hints.** Each event type is its own `ListEvents` stream with a durable cursor. The streams are `DeviceRegistered`, `DeviceDeregistered`, `GroupCreated`, `DeviceAddedToGroup`, `DeviceRemovedFromGroup`, `GroupDeleted`, `DeploymentCreated`, `DeploymentDeleted` and `DeviceStatusReported`. An event only names an object to re-read.
- **Authoritative state.** The stored state comes from the objects themselves, so a duplicated or reordered event cannot corrupt it:
  - the Device object, for owner and key;
  - the group object, for admin and members;
  - the DeploymentRecord, plus its target, recipe-digest and receipt fields.
  Legacy `StatusesKey` statuses are never used, because old package versions can forge them.
- **Tombstones.** An object that disappears (deleted or wrapped) becomes a tombstone (`deleted: true`). Lists hide tombstones unless asked with `include_deleted`.
- **Atomic pages.** A page's rows, change-feed entries and stream cursor commit in one SQLite transaction. A crash at any point resumes without loss or duplication.
- **Type origins.** Origins come from the call package's own type origin table, and are checked against the manifest. Fields introduced by later upgrades are read only once the package defines them.

## Deployment

```bash
suifleet-indexer serve --manifest deployment.json --db /var/lib/suifleet-indexer/fleet.db \
  --tokens /etc/suifleet-indexer/tokens [--listen 127.0.0.1:8650] [--interval-seconds 2]
```

**Startup.**
- The indexer reads the same deployment manifest as the dApp and the daemon ([DEPLOYMENT_MANIFEST.md](DEPLOYMENT_MANIFEST.md)), and verifies it against the chain first: chain identity, package version and origins, UpgradeCap, and registry.
- It catches up before serving, so the API never answers from an empty index.
- It then indexes every `--interval-seconds`, retrying a failed stream without advancing that stream's cursor.

**Network.**
- Run one instance per network and database.
- Bind to loopback, and put a TLS-terminating reverse proxy in front for remote clients. The service speaks plain HTTP.

**Resources.**
- The gRPC endpoint is the manifest's `rpc_url`. It must serve indexed `ListEvents` (Sui ≥ 1.81 in our tests) and retain history back to the first fleet event (see Retention).
- Writes are single-writer. API reads open their own read-only connections: WAL readers do not block the indexer.

**Limits.**
- No hosted instance exists. A systemd unit and packaging are not provided yet.
- The dApp still reads the chain directly. Switching it to the API is follow-up work.

## Auth

**Token file.** `--tokens FILE` holds one `read|operate TOKEN` per line. Tokens must be at least 32 characters, are compared in constant time, and `#` starts a comment. Generate them with, for example, `openssl rand -hex 32`. Keep the file `0600`, owned by the service user.

**Scopes.** Clients send `Authorization: Bearer TOKEN`.
- `read` covers `GET`.
- `operate` also covers `POST /v1/transactions/*`.
- A missing or unknown token gets 401; a valid token with too little scope gets 403.

**Local only.** `--public-read` allows unauthenticated `GET` and still requires `operate` for `POST`. Do not combine it with a non-loopback listener.

**What `operate` can and cannot do.** An `operate` token only lets a client ask the service to *build* a transaction. Signing always happens elsewhere, with the operator's wallet or `ctl --key`. Chain authority is unchanged: group admin, device owner and verified recipes are enforced by the contracts, and pre-checked by the API.

## API (`/v1`, JSON)

| Request | Returns |
|---|---|
| `GET /v1/status` | Chain, call package, tip and lowest retained checkpoint, last change sequence, per-stream checkpoint and lag |
| `GET /v1/devices?owner=&group=` | Devices (owner, key address, arch, current groups) |
| `GET /v1/groups?admin=&device=` | Groups and members |
| `GET /v1/deployments?creator=&component=&group=&device=` | Deployments. `device=` matches direct targets and groups whose admin created the deployment, the same rule devices apply |
| `GET /v1/{devices,groups,deployments}/{id}` | One item. A deployment adds `resolved_devices` and authoritative `receipts` |
| `GET /v1/changes?after=SEQ&limit=&wait=SECONDS` | Integration feed: `{seq, kind, object_id, checkpoint, payload}` in order. Long-polls up to 30 s. Resume with `after=next` |
| `POST /v1/transactions/{add-devices,remove-devices,create-deployment}` | `{transaction, digest, sender, summary}`: base64 BCS of the sign-ready transaction, with gas and versions resolved by node simulation |

**Pagination.**
- Lists take `limit` (default 100, maximum 500) and return `{items, next}`. Pass `cursor=next` until `next` is null.
- Pagination is keyset, by object ID, so concurrent changes never duplicate or skip a stable item.
- Add `include_deleted=true` to list tombstones.

**Bulk requests.**
- Each is checked against the index before building, and rejected with 400 and a reason.
  - **Group changes:** the sender must be the group admin.
  - **Adds:** devices must be the sender's, and not already members.
  - **Deployments:** need the sender's own targets and lowercase-hex SHA-256 digests.
- At most 200 calls per transaction.

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

**Before signing, `--key` checks the returned transaction against the operator's own manifest.** It refuses unless all of these hold:
- the key is the `--sender`'s key, and the transaction's sender;
- gas is paid by the sender and stays within budget;
- the bytes match the API's digest;
- every command is a call into the manifest's package, to a function the operation allows (for example only `device_group::add_device` for `add-devices`), with no type arguments.

**Signing and executing.** The key file holds a 32-byte hex ed25519 seed. Success is read from the execution effects.

## Storage, retention and recovery

**Storage.**
- One SQLite file, in WAL mode with full sync. Back it up with `sqlite3 fleet.db ".backup copy.db"` while running.
- Losing it loses nothing on chain: re-run `index` against an archival node to rebuild.
- About 1 KB per indexed object plus the change feed. The feed is never pruned yet; plan retention per deployment.

**Network reset.** If the database is bound to another chain identifier, the indexer refuses to run: "the network may have reset". `--reset` discards the index and rebuilds it for the manifest's chain.

**Pruned history.** If the node no longer holds a stream's resume checkpoint, indexing stops with an explicit error rather than skipping events. Two ways out:
- Point `rpc_url` at a node that retains the history. This is preferred.
- `suifleet-indexer index ... --rebuild-from-objects` re-reads every *known* object and restarts all streams at the node's lowest retained checkpoint. Objects created during the pruned gap and never indexed stay unknown until they change again. Use this only knowingly.

**Reorgs.** Sui checkpoints are final, so there are no reorgs. The relevant hazards are resets and pruning, handled above.

**Crash safety.** A crash mid-page leaves neither rows nor cursor, and the page is re-read.

## Verified locally (2026-10-14)

- `make indexer-test`: 13 unit tests. They cover:
  - idempotent writes and the change feed;
  - a rolled-back page leaving neither rows nor cursor;
  - keyset pagination and filters;
  - creator-only group resolution;
  - token scopes;
  - bulk validation;
  - verify-before-sign rejections: wrong function, other package, non-call command, sender, gas owner and budget;
  - pruned-history refusal;
  - node JSON integers.
- `scripts/test_indexer_localnet.py` on a real Sui 1.81.1 localnet, all PASS:
  - the API matches chain state (owners, members, targets, resolved devices, a signer-checked receipt) under limit-2 pagination;
  - no or unknown token gets 401, `read` cannot operate (403), and an invalid bulk request gets 400;
  - after a restart, the removal and deletion made while stopped appear once and earlier changes are not repeated;
  - `ctl add-devices` writes an unsigned file, then verifies, signs and executes (effects success), and the indexer shows the new membership;
  - the CLI change feed is in sequence order;
  - load: 400 concurrent paginated reads (8 clients) over about 45k rows had p50 18 ms and p95 88 ms (debug build, laptop-class host);
  - a database bound to another chain is refused, and `--reset` re-indexes.

**Not yet done:**
- testnet, or archival nodes with real retention;
- desired state (SF-013) and identity (SF-008) streams, to be added after those merge;
- dApp migration onto the API;
- packaging.
