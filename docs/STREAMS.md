# Durable streams and export (SF-034)

Components store data in bounded local streams through the daemon's IPC socket ([COMPONENT_IPC.md](COMPONENT_IPC.md)). Data survives crashes and restarts. Streams the operator declares can be exported to an HTTP(S) destination, with retries and resumable checkpoints. The design follows AWS Stream Manager and the SF-028 specification ([GREENGRASS_COMPATIBILITY.md](GREENGRASS_COMPATIBILITY.md#streams-sf-034)). Stream data never goes on chain or to Walrus.

## Configuration

```toml
[ipc.components.sensor]
uid = 1101
operations = ["stream_append", "stream_read", "stream_describe", "stream_create", "stream_list"]
streams = ["telemetry", "sensor-*"]       # names it may use; `*` is a glob

[streams]
max_streams = 32                          # 1..=1024
max_total_bytes = 67108864                # sum of every stream's max_size

[[streams.definitions]]
name = "telemetry"                        # 1-255 of [A-Za-z0-9 ,._-]
max_size = 16777216                       # 1 KiB..=1 GiB of message bytes
strategy_on_full = "RejectNewData"        # or "OverwriteOldestData"
persistence = "File"                      # or "Memory"
# time_to_live_seconds = 86400            # retire older messages
# discard_exported = true                 # retire messages once exported

[streams.definitions.export]
url = "https://ingest.example/v1/streams"
bearer_token_file = "/etc/suifleet/stream-export.token"   # optional, 0600
batch_max_messages = 100                  # 1..=1000
batch_max_bytes = 1048576                 # 1 KiB..=16 MiB (one message always fits)
batch_interval_seconds = 5                # idle poll, 1..=3600
max_backoff_seconds = 300                 # retry backoff cap, 1..=3600
```

`suifleet --check-config` and daemon startup reject invalid definitions, duplicate names, and definitions that exceed the quota. Declared streams are created at startup, or updated to match the configuration. Their persistence cannot change; delete and recreate the stream to change it. **Exports are operator configuration only:** a component can create local streams that its globs allow, but it cannot attach an export or delete a declared stream.

## Semantics

- **Numbering.** Sequence numbers start at 0 and increase by one per append. A number is never reused, even after eviction, a restart or a crash.
  - Numbers are reserved in blocks of 1024 that are persisted before use. A clean shutdown records the exact next number.
  - After a crash, or after a corrupt segment, numbering resumes at the end of the reserved block, so a crash leaves a gap of fewer than 1024 numbers.
  - Receivers deduplicate by sequence, so reusing a number would silently drop new data; skipping ahead is the safe choice.
- **When full.** `RejectNewData` refuses an append with `full`. `OverwriteOldestData` evicts the oldest messages, one at a time, until the new one fits. `max_size` counts message bytes.
- **Retention.** `time_to_live_seconds` retires messages older than the limit. `discard_exported` retires messages once the export checkpoint has passed them. Without either option, messages leave a stream only through the full-stream strategy.
- **Reads** take `{start, min, max}`, defaulting to 0, 1 and 1, with `1 ≤ min ≤ max ≤ 1000`.
  - A read below the oldest retained message returns `evicted` with `oldest`, so consumers detect the loss.
  - Fewer than `min` messages at or after `start` returns `not-enough-messages`.
  - Reads do not consume: each consumer keeps its own cursor.
- **Persistence.**
  - `File` streams append CRC-framed records to segment files under `<state_dir>/streams/<hex name>/`, and sync each append before returning its sequence number. On open, a torn record at the end is truncated. A corrupt record ends its segment, and the result is counted in `corrupt_segments`. Segments are deleted once all their messages are retired. Disk use is at most `max_size` plus one segment, which is `max_size/8` clamped to 4 KiB–4 MiB.
  - `Memory` streams keep their messages in memory and lose them on restart. Their numbering continues as above.
- **Quota.** A stream reserves its `max_size` against `max_total_bytes`, so creating it fails with `quota` if the sum would exceed the limit, or if `max_streams` is reached.
- **Messages.** A message is 1 byte up to min(`max_size`, 1 MiB) in the store. Over IPC, the payload is any JSON value, and its compact JSON bytes are what is stored and counted. The 64 KiB IPC frame limits a single append.

## IPC

| Operation | `topic` | `payload` | Result |
|---|---|---|---|
| `stream_create` | name | `{max_size, strategy_on_full, persistence?, time_to_live_seconds?}` | `{created}`; `exists`, `invalid`, `quota` |
| `stream_append` | name | any JSON value | `{sequence}`; `full`, `invalid`, `not-found` |
| `stream_read` | name | `{start?, min?, max?}` | `{messages: [{sequence, appended_ms, payload}]}`; `evicted` (+`oldest`), `not-enough-messages` |
| `stream_describe` | name | — | `oldest`, `newest` (last assigned), `exported` (−1 before any), `bytes`, `messages`, the definition and export status (destination without query string, batches, failures, last error) |
| `stream_delete` | name | — | `{deleted}`; `declared` for operator streams |
| `stream_list` | — | — | Names the caller's globs match |

Every operation except `stream_list` requires a `streams` glob in the caller's ACL that matches the stream name. Unlike other IPC failures, stream outcomes are returned as their codes, because they are part of the protocol. Authorization failures stay `request_failed`. The Python SDK raises `IpcError(code)`:

```python
from suifleet_ipc import Client, IpcError
sensor = Client('/run/suifleet/ipc.sock', component='sensor')
sequence = sensor.stream_append('telemetry', {'t': 21.5})
for message in sensor.stream_read('telemetry', start=sequence, max_messages=10):
    print(message['sequence'], message['payload'])
```

## Export

One exporter per declared export sends the messages after the stream's checkpoint, oldest first. Each request:

```
POST <url>
content-type: application/json
idempotency-key: <device id>:<hex stream name>:<first>-<last>
x-suifleet-manifest: base64(manifest JSON)
x-suifleet-signature: base64(Sui personal-message signature of the manifest)
authorization: Bearer <token>            (only with bearer_token_file)

{"schema":1,"device_id":"0x…","stream":"telemetry","lost_before":0,
 "messages":[{"sequence":0,"appended_ms":…,"data":"<base64 of the stored bytes>"}]}
```

**The manifest** is canonical JSON with these fields:

```
{schema: 1, kind: "stream-export", chain_identifier, registry, device_id, signer,
 stream, first_sequence, last_sequence, messages, lost_before, body_sha256, created_ms}
```

The device key signs it as a Sui personal message, which can never be valid as a transaction. With the key in the signing service (SF-053), the service signs only a canonical, bounded manifest for this chain, registry and key, and only for the daemon's main process.

**A receiver should:**
1. check the body's SHA-256 against `body_sha256`;
2. verify the signature, for example with Mysten's `verifyPersonalMessageSignature`;
3. check that the signer is the device's current registry identity;
4. deduplicate by (device, stream, sequence).

**Delivery.**
- **At least once, in order.** The checkpoint advances only after a 2xx answer.
- **Retries.** Any other answer, a timeout or a network error keeps the checkpoint and retries the same start, backing off from 1 s to `max_backoff_seconds`.
- **Restarts.** A daemon restart or crash resumes from the persisted checkpoint.
- **Duplicates.** A batch the destination stored, but whose answer was lost, is sent again. If more messages arrived in the meantime, the retry may cover a longer range.
- **Lost before export.** Messages retired before they could be exported, by overwrite or time-to-live, are counted in `lost_before` of the next batch. They are never silently skipped.
- **Permanent refusals.** A 4xx is retried like any other failure: the stream backs up rather than discarding data. The status shows the last error.

**Cost and limits.** Each export makes one request per batch, carrying at most `batch_max_messages` messages and `batch_max_bytes` bytes, with base64 adding about a third. While data is flowing, a stream sends at most one batch at a time, back to back; when idle, it polls every `batch_interval_seconds`. Signing one manifest per batch is the only use of the device key.

## Differences from AWS Stream Manager

- **Native API.** Streams are reached through native IPC operations and the Python SDK, not the Stream Manager SDK or its wire protocol.
- **Eviction and numbering.** Eviction is per message, where AWS evicts whole segments. A crash can skip sequence numbers, but never reuses them.
- **Export targets.** Exports go to HTTP(S) destinations with device-signed manifests. Kinesis Data Streams and S3 are the SF-058 adapter; SiteWise is SF-064. The `export` definitions here are the operator's, not the component's.
- **Retention options.** `time_to_live_seconds` and `discard_exported` exist; AWS's `flushOnWrite` (sync every append) is always on for `File` streams.

## Evidence

| Command | Covers |
|---|---|
| `make edge-test` | All 5 `interfaces/compat/stream-cases.json` sequences (45 steps) against the store, including restart. Crash recovery: a torn tail truncated, records after a corrupt one dropped and their numbers never reused, a clean close exact. Overwrite: segment reclamation, eviction reproduced after a crash. Time-to-live, quota, `discard_exported`, export batches and `lost_before`. Memory streams after a crash. Manifests binding the body, checked by the signing service. IPC request handling. CRC check value |
| `make test-streams` | The real daemon as root with two real component UIDs, the chain simulator and a local export receiver, without network. ACLs, quota, operator-only exports and declared streams. Offline buffering: 25 messages exported in order in signed batches after the destination returns. Interrupted export: a batch stored but answered 500 is retried with the same Idempotency-Key, and the checkpoint advances only after a 2xx. Crash: kill -9 with unexported messages plus a torn tail; everything intact, numbering resumes without reuse, export resumes. Clean restart: numbering exact. `full` from a full stream. Overwrite retention while offline: `evicted` reads, and the export reports the retired count. The receiver checks every body hash and verifies every signature independently (BLAKE2b intent digest and RFC 8032 Ed25519 in Python), and rejects forged manifests. Receipt: [sf034-streams.json](validation/sf034-streams.json) |

**Not yet covered:**
- AWS export targets (SF-058, SF-064);
- a disk-full host filesystem beyond the configured quota;
- a receiver that verifies against the live registry (#11);
- physical Pi power loss (#32).
