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
| `deployment-failed` | critical | The device's on-chain receipt for a deployment is Failed |
| `device-offline` | warning | The device stopped reporting |
| `device-degraded` | warning | The device reports problems, or its report is delayed or inconsistent |
| `deployment-pending` | warning | An online device has not reported a deployment older than `--stuck-after-minutes`. It may be paused or scheduled (deployment control) |

Revoked, deregistered and unactivated devices raise none, because their telemetry is not trusted. `--alert-webhook` posts `{"alerts": [new…], "resolved": [keys…], "sent_ms"}` when something changes. Each alert is sent once. A failed delivery is retried at the next interval and nothing is marked sent until the receiver accepts it.

**CLI** (token from `--token-file` or `SUIFLEET_API_TOKEN`):

```bash
suifleet-indexer ctl alerts [--owner ADDR] [--group ID]
suifleet-indexer ctl logs DEVICE [--q TEXT] [--source component:NAME] [--since-ms MS] [--max N]
suifleet-indexer ctl diagnostics DEVICE --out bundle.json     # written 0600, refuses to overwrite
suifleet-indexer ctl audit [--limit N]
```

## dApp

The dashboard lists the connected owner's alerts, refreshed every 30 s, using the same indexer URL and per-tab `read` token as device health. Every alert is validated strictly: kinds, severities and text bounds. Any invalid alert makes the whole list unavailable rather than partial. A failed or stalled refresh shows alerts as unavailable instead of an old list.

Unlike device health, the dApp cannot re-derive alerts from the chain alone, so the panel says "Reported by the fleet indexer". Log search and bundles are not in the dApp: they need a `support` token, and go through the CLI.

## Evidence

| Command | Covers |
|---|---|
| `make edge-test` | The shared redaction cases. Tailing: complete lines once, rotation into `.1`, two unseen rotations reported lost, inode reuse, the budget, overlong lines read whole or omitted (also across intervals), an unterminated rotated tail followed by the current file. The rotating daemon log. Batch record and byte limits, sized from each record's exact JSON (control bytes escape to six bytes), and a backlog of control-character lines sealed and shipped. Collection redacting a confidential overlay value with cursors persisted, and never splitting a value across records with a one-line budget. A multiline overlay value and a complete PEM private-key block (a body without `+`, `/` or `=`) shipped one line per interval across restarts with nothing exported; an unterminated key block closing. Collection stopping, across restarts, while an overlay is truncated, unreadable or unlistable, then shipping the held line redacted once it is restored. The signing service's log-batch checks (other chain, registry, signer or schema, empty, overlong, bad source, non-canonical, transactions). `[logs]` config bounds and the need for `[telemetry]` |
| `make indexer-test` | The shared redaction cases. Ingest (accepted, duplicate, reused seq, other chain or key, bounds, clock, unindexed retry, revoked, tampered). Retention, search, paging and output redaction. Audit. Alert rules over real stored telemetry and receipts. Webhook deduplication and retry against a real local HTTP receiver. HTTP scopes: no token 401, `read` 403 on logs, `support` served, audit `operate`-only with fingerprints |
| `python3 scripts/test_logs.py` (in `make test-ota`) | The real daemon and simulator. Batches spooled while the ingress is down. A component's overlay value, an api key and a raw key redacted on the device. 120 lines shipped in order over several intervals within the budget. The daemon's own log shipped. No line sent twice across a daemon restart. Every batch verified with Mysten's SDK |
| `node scripts/test_alerts.cjs` (in `make test-manifest`) | Strict parsing, an owner-scoped authenticated fetch, unavailable instead of stale or partial lists |
| `make test-indexer-localnet` | Real daemons, a real localnet and the real indexer. The activated device's redacted daemon log is searchable with a `support` token, and a `read` token is refused. The revoked device's batches are rejected. The diagnostics bundle shows the real on-chain receipt. Alerts are served. Support access is audited |

**Not yet covered:**
- a hosted indexer with TLS, per-client rate limiting and webhook signing;
- log search in the dApp;
- physical Pi logs and power loss (#32);
- public-network operation (#11).
