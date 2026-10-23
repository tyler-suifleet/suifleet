# Remote logs, diagnostics and alerts (SF-020)

Devices ship their logs, redacted and signed, to the fleet indexer, next to telemetry ([TELEMETRY.md](TELEMETRY.md)). Support staff search them and download diagnostic bundles with a separate `support` token, and every such access is audited. The indexer derives actionable alerts from verified telemetry and the indexed on-chain receipts, serves them to the dApp and the CLI, and can post them to a webhook. Keys, recovery phrases and confidential component configuration are never exported.

## On the device

```toml
[telemetry]
url = "https://fleet.example"        # logs go to {url}/v1/logs

[logs]
interval_seconds = 60                # 10..3600
max_lines_per_interval = 512         # 1..10000; the rest waits for later intervals
buffer_max = 256                     # batches kept while the indexer is unreachable
```

**Sources and rotation:**
- With `[logs]`, the daemon also writes its own log to `<state_dir>/logs/daemon.log`. That file rotates at 1 MiB, keeping one `.1` file.
- Each supervised component already logs to `<state_dir>/supervised/<name>/component.log`, which also rotates at 1 MiB (SF-014).
- A shipper tails all of them. Its per-source cursors record the inode, offset and first bytes, so a cursor follows a rotation into the `.1` file and survives inode reuse.
- Every line is read once, also across daemon restarts. Lines lost to a rotation the shipper never saw are replaced by a marker line and counted.

**Redaction, before a line leaves the daemon.** The rules are in [redact.rs](../suifleet-edge/src/redact.rs), with shared cases in [log-redaction-cases.json](../interfaces/log-redaction-cases.json). Each match is replaced by `[REDACTED]`:
- Sui private keys (`suiprivkey1…`);
- PEM private-key blocks: every line from a `BEGIN … PRIVATE KEY` marker to its `END` marker, whatever the body holds. The shipper keeps this state in the source's cursor across intervals and restarts, and closes a block after 256 lines if its end marker is missing. Any whole line of 64 or 70 base64 characters (PEM and OpenSSH key body width) is also dropped, even outside a block;
- values after secret-like names: `password`, `token`, `api_key`, `secret`, `pin`, `mnemonic`, `seed`, `private_key`, `credential`, `authorization`, and names ending in those, such as `DB_PASSWORD`. Quoted or not, and a backslash-escaped quote (as in JSON) does not end a quoted value; an `Authorization` scheme such as `Bearer` is kept;
- `Bearer` tokens;
- 64 or more hex digits not written as a `0x` object ID or address;
- base64 runs of 40 or more characters that contain `+`, `/` or `=`. Base58 digests and base64url blob IDs are kept;
- runs of 12 or more words of 3 to 8 lowercase letters, which is how recovery phrases look;
- every value in the device's confidential configuration overlays (SF-015), exactly and however short. A short value such as a PIN is redacted wherever it appears, even inside other words. A value that spans lines is also matched line by line (each non-blank line, as written and trimmed), because the shipper reads physical lines.

  Loading these values fails closed. If an overlay cannot be listed, read or parsed (for example, while it is being edited), the interval collects nothing: no line is read, spooled or advanced past. The daemon logs a warning naming only the file, and retries next interval. A running component keeps its old secrets during an edit, so shipping without them could export them.

  Replaced and removed values stay redacted. Unread log lines, and a component still running with its materialized configuration, can hold a value after it leaves the overlays, including a value the shipper never saw. The daemon therefore records every confidential value in `<state_dir>/logs/confidential.json` (mode 0600, never shipped), whether or not `[logs]` is configured:
  - Before materializing a configuration for a component, install, validate or recovery command, the daemon adds the component's overlay values to this file. If it cannot record them, the materialization fails. A value replaced between two intervals is therefore still known.
  - Each interval reads its lines first and loads the values afterwards. A value that a line can hold was recorded before the line was written.
  - The shipper also adds the confidential values of every component version's materialized `configuration.json`: each leaf that differs from that version's public `configuration-state.json` record (every leaf, without one). This covers logs enabled on an existing installation. If a materialized configuration cannot be read or parsed, collection stops, as for an overlay.
  - A value that is in no overlay and no materialized configuration is kept for at least one more hour, and until an interval has read every log to its end.
  - The daemon creates the file on its first start, before any component runs. Component log lines written before that (by an older daemon that recorded nothing) are never shipped: shipping starts where each existing component log ended.
  - If the file, or the shipper's cursors in `logs/shipper.json`, cannot be read or parsed, collection stops. A lost cursor would read old lines again after their values may have been forgotten.

A whole line is redacted before it is truncated, so a secret is never split across records. A line longer than 16 KiB is replaced by a marker line rather than shipped in pieces. When a rotated file ends without a newline, its last line is shipped as it is, and the shipper moves on to the current file.

The rules lean towards over-redaction. They are a safety net; components still must not log secrets.

**Bounds:**
- lines at most 1024 bytes after redaction;
- batches at most 256 records and 64 KiB;
- the per-interval line budget;
- the spool bound. Batches dropped from a full spool are counted in later batches' `dropped_lines`.

**Signature:**
- A batch is canonical schema-1 JSON with `kind: "logs"`, bound to the chain, registry, device and signing key, with a strictly increasing `seq`.
- It is signed as a Sui personal message, like telemetry, so it can never be valid as a transaction.
- With the key in the signing service (SF-053), the service signs only such a canonical, bounded batch for this chain, registry and key, and only for the daemon's main process.

**CloudWatch Logs.** The same daemon and component logs can also be exported to Amazon CloudWatch Logs by the separate SF-060 adapter, which reads them under its own user with `[runtime] log_group_read = true`. See [CLOUDWATCH_LOGS.md](CLOUDWATCH_LOGS.md).

## Indexer

```bash
suifleet-indexer serve --manifest deployment.json --db fleet.db --tokens tokens \
  --telemetry-db telemetry.db [--logs-retain 20000] [--stuck-after-minutes 60] \
  [--alert-webhook https://ops.example/hook [--alert-interval-seconds 60]]
```

**Ingress.** `POST /v1/logs` takes no token. It applies the same checks as telemetry:
- the signature over the exact bytes, with ed25519 or P-256 only;
- the batch names its signer, which must be the device's current registry identity (not revoked);
- the chain and registry binding;
- the batch bounds;
- a clock at most 5 minutes ahead;
- a new `seq`.

Unindexed devices get `retry`; any other failure is `rejected`. The newest `--logs-retain` lines per device are kept, in the telemetry database. `--reset` clears them.

**Token scopes.** `read` < `support` < `operate`.

| Request | Scope | Returns |
|---|---|---|
| `GET /v1/devices/{id}/logs?q=&source=&since_ms=&before=SEQ:IDX&limit=` | `support` | Lines newest first; `q` is a case-insensitive substring; `source` is `daemon` or `component:NAME` |
| `GET /v1/devices/{id}/diagnostics` | `support` | A bundle: the device record, health, its 5 newest signed reports, deployments with this device's receipt, alerts, log stats and its last 500 lines |
| `GET /v1/support/audit?limit=` | `operate` | Support accesses: time, route, device, query, and the token's fingerprint (never the token) |
| `GET /v1/alerts?owner=&group=` | `read` | Alerts, critical first |

Support routes are never public, not even with `--public-read`. Every line they serve is redacted again with the daemon's rules, so data from an older daemon is covered too. Bundles are assembled from stored data: no command or shell reaches a device.

**Alerts.** Each alert has a stable `key`, a `severity`, a message and an `action` for the operator:

| Kind | Severity | When |
|---|---|---|
| `component-broken` | critical | A supervised component exhausted its restart budget |
| `deployment-failed` | critical | The device's on-chain receipt for a deployment is Failed, and the device has not demonstrably recovered that component (below) |
| `device-offline` | warning | The device stopped reporting |
| `device-degraded` | warning | The device reports problems, or its report is delayed or inconsistent |
| `deployment-pending` | warning | An online device has not reported a deployment older than `--stuck-after-minutes`. It may be paused or scheduled (deployment control) |
| `messaging-backpressure` | warning | A subscriber's pub/sub queue has been at least 90 % full for two heartbeat intervals; publications to it are refused |
| `messaging-loss` | warning | Publications were refused or QoS 0 messages dropped, or the MQTT bridge refused or rejected inbound messages, since the previous report |
| `bridge-disconnected` | warning | The MQTT bridge has been disconnected for two heartbeat intervals |
| `stream-near-limit` | warning | A stream is at least 90 % full and would reject data or overwrite unexported messages, or it rejected appends since the previous report |
| `stream-data-lost` | critical | Stream messages were retired before they were exported, since the previous report |
| `export-failing` | critical | A stream export failed three consecutive times |
| `shadow-sync-failing` | warning | A shadow sync failed three consecutive times |
| `shadow-conflict` | warning | Keys changed on both sides were settled by the configured owner, since the previous report |

The last eight come from the services section of the device's newest report ([TELEMETRY.md](TELEMETRY.md#service-health-sf-085)) and are raised only while the device is online. An alert about events since the previous report resolves with the next report that has none. `suifleet-indexer telemetry-alerts ENVELOPES.json` shows, offline, which of them a set of signed reports raises (it checks signatures and bounds, not the registry identity).

**Recovered deployment failures (SF-098).** A Failed receipt is history: the receipt and the deployment record are never changed. It stops being an active `deployment-failed` incident once the device demonstrates recovery of the same component. Its newest report must be fresh and trusted (the device is `healthy` or `degraded`, not `offline` or `delayed`). Every entry for that component must show it installed, not broken, running if supervised, and from a deployment that targets this device, has an Applied receipt from it and was created on chain after the failed one. Versions are not compared, so a rollback to an older version is a recovery. A newer deployment that is pending, not yet applied, reported without an Applied receipt, for another component or on another device clears nothing. A failure created after the recovery stays active. If the evidence later becomes unavailable (the device goes offline, its report is delayed or the component breaks), the failure is active again under the same key. The webhook reports a recovered failure as `resolved` under its stable key.

Revoked, deregistered and unactivated devices raise none, because their telemetry is not trusted. `--alert-webhook` posts `{"alerts": [new…], "resolved": [keys…], "sent_ms"}` when something changes. Each alert is sent once. A failed delivery is retried at the next interval and nothing is marked sent until the receiver accepts it.

**CLI** (token from `--token-file` or `SUIFLEET_API_TOKEN`):

```bash
suifleet-indexer ctl alerts [--owner ADDR] [--group ID]
suifleet-indexer ctl logs DEVICE [--q TEXT] [--source component:NAME] [--since-ms MS] [--max N]
suifleet-indexer ctl diagnostics DEVICE --out bundle.json     # written 0600, refuses to overwrite
suifleet-indexer ctl audit [--limit N]
```

## dApp

The dashboard lists the connected owner's alerts as **Active incidents**, refreshed every 30 s, using the same indexer URL and per-tab `read` token as device health. Every alert is validated strictly: kinds, severities and text bounds. Any invalid alert makes the whole list unavailable rather than partial. A failed or stalled refresh shows alerts as unavailable instead of an old list.

Unlike device health, the dApp cannot re-derive alerts from the chain alone, so the panel says "Reported by the fleet indexer". Below it, the dashboard counts the Failed receipts still in the on-chain deployment history, so a recovered failure is visible as history rather than as an incident (SF-098). Log search and bundles are not in the dApp: they need a `support` token, and go through the CLI.

## Evidence

| Command | Covers |
|---|---|
| `make edge-test` | The shared redaction cases. Tailing: complete lines once, rotation into `.1`, two unseen rotations reported lost, inode reuse, the budget, overlong lines read whole or omitted (also across intervals), an unterminated rotated tail followed by the current file. The rotating daemon log. Batch record and byte limits, sized from each record's exact JSON (control bytes escape to six bytes), and a backlog of control-character lines sealed and shipped. Collection redacting a confidential overlay value with cursors persisted, and never splitting a value across records with a one-line budget. A multiline overlay value and a complete PEM private-key block (a body without `+`, `/` or `=`) shipped one line per interval across restarts with nothing exported; an unterminated key block closing. Collection stopping, across restarts, while an overlay is truncated, unreadable or unlistable, then shipping the held line redacted once it is restored. Replaced, removed-file and removed-directory overlay values still redacted in queued lines and in a running component's later output, across restarts, with the retained file 0600. A value held only by a materialized configuration (logs enabled on an existing installation) redacted on the first collection, without its public leaves, and kept after it is gone. Values recorded at materialization and replaced before the next interval redacted. A retired value kept while a materialized configuration holds it, and past the retention while a log is unread, then forgotten. Collection stopping, across restarts, while a materialized configuration is truncated or unreadable, while the retained file is corrupt, or while the cursors are corrupt. Component log lines from before the retained file existed never shipped. The supervisor recording each overlay value before materializing it The signing service's log-batch checks (other chain, registry, signer or schema, empty, overlong, bad source, non-canonical, transactions). `[logs]` config bounds and the need for `[telemetry]` |
| `make indexer-test` | The shared redaction cases. Ingest (accepted, duplicate, reused seq, other chain or key, bounds, clock, unindexed retry, revoked, tampered). Retention, search, paging and output redaction. Audit. Alert rules over real stored telemetry and receipts, including the service-health alerts (raised only while online). Webhook deduplication and retry against a real local HTTP receiver. HTTP scopes: no token 401, `read` 403 on logs, `support` served, audit `operate`-only with fingerprints |
| `python3 scripts/test_logs.py` (in `make test-ota`) | The real daemon and simulator. Batches spooled while the ingress is down. A component's overlay value, an api key and a raw key redacted on the device. 120 lines shipped in order over several intervals within the budget. The daemon's own log shipped. No line sent twice across a daemon restart. Every batch verified with Mysten's SDK |
| `node scripts/test_alerts.cjs` (in `make test-manifest`) | Strict parsing (service-health kinds included), an owner-scoped authenticated fetch, unavailable instead of stale or partial lists |
| `make test-indexer-localnet` | Real daemons, a real localnet and the real indexer. The activated device's redacted daemon log is searchable with a `support` token, and a `read` token is refused. The revoked device's batches are rejected. The diagnostics bundle shows the real on-chain receipt. Alerts are served. Support access is audited |

**Not yet covered:**
- a hosted indexer with TLS, per-client rate limiting and webhook signing;
- log search in the dApp;
- physical Pi logs and power loss (#32);
- public-network operation (#11).

## Direct Sui/Walrus archives (SF-082)

A configured `[fleet_data]` producer sends bounded signed health directly to Sui;
`[fleet_data.archives]` batches redacted logs and owner/support diagnostics into
native Seal-encrypted fleet-prepaid Walrus blobs. The dApp's `/data/` page verifies
the chain index, identities, signatures, ciphertext and read policy before showing
plaintext. The HTTP indexer and its legacy log page remain optional. See
[serverless fleet data](SERVERLESS_FLEET_DATA.md) for explicit object configuration,
retention, costs, keyless codec deployment and honest validation limits.

Push notifications need an optional owner-run relay; an open browser can derive
alerts directly. Neither a relay nor an indexer is a trust root. Unavailable
chain/provider data is never treated as a healthy device or a resolved incident.
