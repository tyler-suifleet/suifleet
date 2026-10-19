# Device shadows and cloud synchronization (SF-033)

A shadow is a named JSON document holding the **desired** state of a device or application and the state it **reported**, with a version. Components read and write shadows through the daemon's IPC socket ([COMPONENT_IPC.md](COMPONENT_IPC.md)). Shadows survive crashes and restarts. An optional adapter synchronizes selected shadows with a cloud shadow service, settling conflicting edits by an explicit policy. The document semantics follow AWS IoT Device Shadow and the SF-028 specification ([GREENGRASS_COMPATIBILITY.md](GREENGRASS_COMPATIBILITY.md#shadows-sf-033)). Shadow documents never go on chain or to Walrus.

## Configuration

```toml
[ipc.components.app]
uid = 1101
operations = ["shadow_get", "shadow_update", "shadow_list", "subscribe", "receive", "ack"]
[[ipc.components.app.shadow_policies]]
operations = ["aws.greengrass#GetThingShadow", "aws.greengrass#UpdateThingShadow"]
resources = ["$aws/things/device1/shadow/name/config"]
sections = ["reported"]                  # optional: this grant may write reported only
[[ipc.components.app.pubsub_policies]]   # delta notifications ([pubsub])
operations = ["aws.greengrass#SubscribeToTopic"]
resources = ["$aws/things/device1/shadow/name/config/update/delta"]

[pubsub]                                 # optional; needed for change notifications

[shadows]
thing_name = "device1"                   # 1-128 of [A-Za-z0-9:_-]
max_shadows = 64                         # 1..=128 existing shadows

[shadows.sync]                           # optional cloud synchronization
url = "https://shadows.example/v1/sync"
bearer_token_file = "/etc/suifleet/shadow-sync.token"   # optional, 0600
shadows = ["", "config"]                 # names to synchronize; "" is the classic shadow
interval_seconds = 30                    # cloud poll, 1..=3600; local changes sync at once
max_backoff_seconds = 300                # retry backoff cap, 1..=3600
desired_conflict = "cloud"               # who wins a desired key both sides changed
reported_conflict = "device"             # who wins a reported key both sides changed
```

`suifleet --check-config` and daemon startup reject an invalid thing name, limits, policies, sync URL (it must be http(s) without embedded credentials) and duplicate or invalid sync names.

## Documents

- **Names.** The classic shadow is `""`; named shadows are 1–64 characters of `[A-Za-z0-9:_-]`.
- **Requests.** An update is `{state: {desired?, reported?}, version?, clientToken?}`; any other key is `invalid`. Updates merge recursively into the stored document:
  - a `null` value deletes its key, and a `null` section deletes the section;
  - objects merge key by key; arrays and other values replace whole; arrays may not contain `null`;
  - objects left empty are removed.
- **Delta.** Responses carry `state.delta`: the desired keys whose values differ from reported, with their path from the root. Keys only in reported are ignored. Values compare as JSON types, so `true` differs from `1`.
- **Versions.** Each accepted update or deletion adds one to the version. A request may supply `version`, which must equal the current one or the request fails with `conflict`; this is optimistic concurrency for read-modify-write. A supplied version must be an integer of at least 1, otherwise `invalid`.
- **Deletion.** Deleting a shadow adds one to its version and keeps it, so a recreated shadow continues the sequence and a stale version is still refused. Unlike AWS, there is no 48-hour reset.
- **Limits.** Each section is at most 8 levels deep (the section object is level 1; every nested object or array adds one). The stored `{desired, reported}` document is at most 8 KiB as compact UTF-8 JSON (`too-large`). `clientToken` is at most 64 bytes and is echoed. A rejected request leaves the document and version unchanged.
- **Quota.** At most `max_shadows` shadows exist (`quota` beyond). Deleted shadows keep their version on disk; at most `2 × max_shadows` shadows, existing or deleted, are retained, and creating a new name beyond that fails with `quota` (a deleted name can always be recreated within the existing limit).
- **Persistence.** Each shadow is one file, `<state_dir>/shadows/s-<hex of the name>.json`, holding the document, version and sync state. A change is written to a temporary file, synced and renamed into place before the request returns, so a crash leaves either the old or the new document. Leftover temporary files are removed at startup. A shadow file that cannot be parsed, or more existing shadows than `max_shadows`, stops startup and nothing is removed; inspect the file or raise the limit.

All of this is pinned by [`interfaces/compat/shadow-cases.json`](../interfaces/compat/shadow-cases.json), which the daemon executes.

## IPC

| Operation | `topic` | `payload` | Result |
|---|---|---|---|
| `shadow_get` | name | — | `{state: {desired?, reported?, delta?}, version}`; `not-found` |
| `shadow_update` | name | update request | `{state, version, clientToken?}`; `conflict`, `invalid`, `too-large`, `quota`, `unavailable` (storage failure) |
| `shadow_delete` | name | null or `{version}` | `{version}`; `not-found`, `conflict`, `invalid` |
| `shadow_list` | — | — | `{shadows: [names]}`: existing named shadows a `ListNamedShadowsForThing` grant matches (the classic shadow is not listed, as in AWS) |
| `shadows` | — | null or `{shadow: name}` | Administrator only. Every existing shadow's version, size, cloud version and sync counters, and notification counters; with a name (deleted shadows included), also its last sync error and up to 16 conflicting paths of its last sync |

**Authorization** mirrors `aws.greengrass.ShadowManager` policies. Operations are `aws.greengrass#GetThingShadow`, `UpdateThingShadow`, `DeleteThingShadow`, `ListNamedShadowsForThing` or `*`. Resources are `$aws/things/<thing>/shadow` (classic) or `$aws/things/<thing>/shadow/name/<name>`, globs in which only `*` is special. Any one matching policy grants. The SuiFleet `sections` field scopes an update grant: every section an update names must be in the same policy's `sections`, so a component can be limited to writing `reported`. The operation must also be in the component's `operations` list, `shadows` is never grantable to a component, the principal is the kernel-authenticated UID, and anything else is denied. Authorization failures are `request_failed`; shadow outcomes are returned as the codes above. The Python SDK raises `IpcError(code)`:

```python
from suifleet_ipc import Client, IpcError
app = Client('/run/suifleet/ipc.sock', component='app')
shadow = app.shadow_get('config')
try:
    app.shadow_update('config', {'reported': {'mode': 'run'}}, version=shadow['version'])
except IpcError as error:
    if str(error) == 'conflict':
        pass  # re-read and retry
app.subscribe('$aws/things/device1/shadow/name/config/update/delta')
change = app.receive('$aws/things/device1/shadow/name/config/update/delta')
```

## Notifications

With `[pubsub]`, every change is published into local pub/sub by sender `@shadows`, on the AWS shadow topics of the thing (`<prefix>` is `$aws/things/<thing>/shadow` or `$aws/things/<thing>/shadow/name/<name>`):

| Topic | When | Payload |
|---|---|---|
| `<prefix>/update/delta` | the delta is non-empty and differs from the previous delta | `{state: <delta>, version, timestamp}` |
| `<prefix>/update/documents` | every update | `{previous: {state, version} \| null, current: {state, version}, timestamp}` |
| `<prefix>/delete/accepted` | deletion | `{version, timestamp}` |

Changes pulled from the cloud are announced the same way, so a component follows desired state by subscribing to its delta topic (QoS 1 by default: durable until acknowledged). `timestamp` is in seconds. Pub/sub payloads are at most 4 KiB, so a larger notification carries only the versions and `truncated: true`; read the shadow. The change is stored before it is announced. A notification refused by a full subscriber queue is logged and counted in the `shadows` status (`notifications.refused`) and is not retried; subscribers recover by comparing versions with `shadow_get`. Without `[pubsub]` there are no notifications; poll the version.

## Cloud synchronization

The adapter keeps each listed shadow equal to a document held by a cloud shadow service, through a small HTTP(S) protocol. It is a SuiFleet protocol; AWS IoT Device Shadow synchronization is the `aws-iot-core-connection` adapter (SF-061).

**Requests.** Each is a `POST` to `url` with a JSON body and the same device-signed headers as stream exports ([STREAMS.md](STREAMS.md#export)):

```
content-type: application/json
x-suifleet-manifest: base64(manifest JSON)
x-suifleet-signature: base64(Sui personal-message signature of the manifest)
authorization: Bearer <token>            (only with bearer_token_file)

{"schema":1,"op":"get","device_id":"0x…","shadow":"config"}
{"schema":1,"op":"put","device_id":"0x…","shadow":"config","expected_version":7,"document":{"desired":{…},"reported":{…}}}
```

The manifest is canonical JSON, `{schema: 1, kind: "shadow-sync", chain_identifier, registry, device_id, signer, shadow, op, body_sha256, created_ms}`. With the key in the signing service (SF-053), the service signs only such a canonical manifest for this chain, registry and key. A service should check the body hash, verify the signature (for example with Mysten's `verifyPersonalMessageSignature`), check that the signer is the device's current registry identity, and reject stale `created_ms`.

**Responses.**
- `get`: `200 {"version": N, "document": {…} | null}`. `version` is the cloud's own counter (0 if the shadow never existed); `null` means absent or deleted.
- `put`: compare-and-swap. If `expected_version` equals the current version, store the document (`null` deletes) and answer `200 {"version": M}` with `M` greater than the expected version; otherwise `409`. Redirects are not followed; anything else is a failure.

The adapter refuses a cloud document that is not `{desired?, reported?}` objects, contains `null`, or exceeds the document limits, and an answer over 64 KiB.

**Reconciliation.** For each shadow the device stores a **base**: the document both sides last agreed on, and the cloud version it had. Each cycle reads the cloud document, then merges three ways:
- a key changed on one side only since the base takes that side's value (additions and deletions included);
- a key changed on both sides to different values is a **conflict**, settled by the section's owner: `desired_conflict` (default `cloud`) and `reported_conflict` (default `device`);
- objects merge key by key, other values whole, so concurrent edits to different keys both survive;
- a shadow deleted on one side and changed on the other keeps the change (deletion never discards edits);
- with no base (first sync), both documents are combined, conflicts settled by owner.

If the merge differs from the cloud document, the adapter puts it with `expected_version` set to the version it read. A `409` means another writer stored first; the adapter reads and merges again on the next attempt, so neither change is lost. It then stores the agreed document and base locally in one atomic write, as a new local version (with notifications) if the document changed. Local edits made while a cycle was in flight are kept on top of the agreed document and pushed in the next cycle; if that combination would exceed the document limits, the cycle fails without changing the local document or base and is retried. A received document is stored as an update would store it (empty objects removed). Conflicts are logged with their paths and counted.

**Offline behaviour.** Components keep reading and writing shadows locally while the service is unreachable; nothing is queued beyond the current document. Failures retry with backoff from 1 s to `max_backoff_seconds`. After a restart, including `kill -9`, the persisted base continues the reconciliation, so offline edits are pushed and unchanged shadows are not rewritten. Every local change wakes the adapter, so edits normally reach the service within one round trip; cloud changes are seen within `interval_seconds`.

**Not provided.** Local versions and cloud versions are independent counters; clients must not compare them. The local `version` check protects only local writers. The service decides its own authorization; the adapter syncs only the operator-listed shadows.

## Differences from AWS

- **Native API.** Native IPC operations and the Python SDK, not the ShadowManager IPC wire protocol. Policies and topics are AWS-shaped.
- **No metadata.** Responses carry no per-key `metadata` timestamps; notifications carry a whole-second `timestamp`.
- **Deletion** keeps the version sequence, with no 48-hour reset.
- **Sync.** The SuiFleet HTTP(S) adapter with three-way merge and per-section owners, instead of AWS IoT Device Shadow; AWS sync is SF-061.
- **Rate limits.** No per-shadow request rate limit; the IPC socket's own bounds apply.

## Evidence

| Command | Covers |
|---|---|
| `make edge-test` | All 54 steps of the 10 `interfaces/compat/shadow-cases.json` sequences against the store, each also after reopening it from disk. Delete-version validation order. Quota for existing and retained deleted shadows, also enforced at startup with data kept. An interrupted replacement keeps the last document; an unreadable file stops startup and is kept. Policies scoped by operation, resource and section, administrator-only status. Delta, documents and delete notifications through the real pub/sub broker, truncation of large notifications, no repeated identical deltas. Three-way merge: one-sided changes, conflicts by owner both ways, deletion versus change, first sync, structural conflicts. Local edits during a sync kept, or the cycle refused with the old base when they cannot merge within the limits. Received documents normalized. Sync state persisted. Sync manifests binding the body, checked by the signing service. Configuration validation. IPC protocol codes |
| `make test-shadows` | The real daemon as root with three real component UIDs, durable pub/sub, the chain simulator and a local reference shadow service, without network. ACLs: operation, resource and reported-only section scope. Delta notification received and acknowledged. Stale and invalid versions, oversize and null-in-array requests refused without a version change. Cloud desired change pulled and announced. Offline edits on both sides reconciled after reconnect with two conflicts settled by owner and one-sided keys kept. A put losing a compare-and-swap to a concurrent cloud writer re-read and merged. Clean stop and `kill -9`: documents, versions and sync base intact, no redundant pushes. An offline edit made before `kill -9` pushed after reconnect. Local deletion propagated, recreation continuing the version, cloud deletion pulled, modify-versus-delete keeping the change, tombstones across restart. The service verifies every body hash and signature independently (BLAKE2b intent digest and RFC 8032 Ed25519 in Python). Receipt: [sf033-shadows.json](validation/sf033-shadows.json) |

**Not yet covered:**
- AWS IoT Device Shadow synchronization (SF-061) and a hosted SuiFleet shadow service;
- a service that verifies the signer against the live registry (#11);
- the AWS IPC wire protocol (`aws_api` remains undecided);
- a disk-full host filesystem beyond the configured quota;
- physical Pi power loss (#32).
