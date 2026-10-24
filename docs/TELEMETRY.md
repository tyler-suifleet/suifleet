# Device heartbeat, inventory and health (SF-019)

> Product policy (SF-137): SuiFleet hosts no application servers. Product health
> and receipts come from Sui, encrypted logs/diagnostics from Walrus, and alerts
> are derived in the browser. See [inventory/migration](SERVERLESS_PRODUCT.md)
> and [setup](SERVERLESS_FLEET_DATA.md). HTTP configuration, API tokens, indexer
> commands, webhooks and HTTP evidence below describe the optional
> **customer-hosted reference integration**, not product requirements.
> The product dApp uses no indexer URL or read token.

This page documents the optional customer-hosted HTTP reference integration.
The product daemon publishes on-chain health with `[fleet_data]`, and the dApp
reads it directly; see [serverless setup](SERVERLESS_FLEET_DATA.md). `[telemetry]`
and `[fleet_data]` are mutually exclusive: their shared signed snapshot schema
uses independent sequence counters, so startup rejects concurrent producers.
The signature itself has no transport domain: customer HTTP ingress must not
authorize identities used for on-chain health, including historical reports.
Remove the old ingress authorization when migrating to the product backend.
External provider, real-wallet and physical acceptance remain separately tracked
in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087](https://github.com/tyler-suifleet/suifleet/issues/159).

Redacted remote logs, diagnostic bundles and alerts build on this path: see [LOGS.md](LOGS.md) (SF-020).

Devices report a heartbeat with their installed components and health. The fleet indexer ([INDEXER.md](INDEXER.md)) receives these reports, and the dApp shows online, last-seen and degraded state for each device.

Telemetry is **off-chain** and high-volume. It never replaces the compact, authoritative receipts that devices report on chain for each deployment. Nothing in this path writes to the chain, and no contract changed.

## What a device sends

With `[telemetry]` configured, the daemon builds a snapshot every `interval_seconds` (default 60, between 10 and 3600). A snapshot has schema 1 and contains:
- **Binding:** chain identifier, registry ID, Device ID, the signing address, and a sequence number `seq`.
- **Timing:** the device clock (`observed_ms`), the interval, the daemon version, the boot ID and the daemon start time.
- **Spool:** `dropped`, the number of snapshots dropped from a full spool so far.
- **Sync:** the poll loop's chain synchronization. That is the last success, the consecutive failures and the last error.
- **Receipts:** `pending_receipts`, the deployment outcomes not yet reported on chain.
- **Components:** every installed component, with its version and deployment. For a supervised component, also its phase, restarts, last exit and transition time.
- **Health:** `healthy` or `degraded`, with `reasons`.
- **Services** (schema 2 only): the health of durable pub/sub, its MQTT bridge, streams and shadow sync, when any of them is enabled. See [Service health](#service-health-sf-085).

`seq` is strictly increasing per device. It is the last sequence number plus one, but at least the clock in milliseconds. A device whose state was wiped therefore continues above what it sent before, as long as its clock is correct. The daemon persists `seq` before it signs the snapshot, so a crash never reuses one.

**Sources.** The snapshot reads only the daemon's durable files, each replaced atomically by its writer:
- `state.json`, the journal;
- `supervised/active.json` and `supervised/status.json`.

The task runs beside the poll loop, so a long apply or a component bootstrap does not silence the heartbeat. A supervised component reports the version committed in `active.json`. That is what actually runs, even after a failed replacement left another version in the journal.

**The device's own judgement.** A snapshot is `healthy` only when there are no reasons. Each of these is a reason:
- chain sync has not completed yet, has not succeeded within three polls plus an hour, or is currently failing;
- a supervised component is not running (`starting`, `backoff`, `stopping`, `stopped`, `exited` or `broken`), with its last exit, or was not started;
- a one-shot component's install was never confirmed (in progress, failed or interrupted);
- a deployment outcome is indeterminate;
- the journal is unreadable.

**Bounds.** These hold for every snapshot:
- at most 64 KiB in total;
- at most 64 components and 16 reasons;
- names, versions and deployment IDs at most 128 bytes;
- reasons and exit or error texts at most 200 bytes.

The daemon truncates longer texts at a character boundary. Components beyond a limit are left out, and a reason says so.

**Signature.** The snapshot's exact JSON bytes are signed with the device key as a Sui personal message: intent `[3, 0, 0]` with the BCS `vector<u8>`. Such a signature can never be valid as a transaction. Both kinds of device key work: an ed25519 key file, and a P-256 key in a PKCS#11 token (SF-021). With the key in the device signing service (`[device.signer] kind = "service"`, SF-053), the daemon asks the service, which signs only a canonical snapshot of the manifest's chain and registry naming its own key, and only for the daemon's main process ([KEY_ISOLATION.md](KEY_ISOLATION.md)). Until the unit records that process, just after the daemon is ready, a heartbeat is refused and the next one is signed. The envelope is `{"message": base64(bytes), "signature": base64(UserSignature)}`.

## Service health (SF-085)

Durable pub/sub ([MESSAGING.md](MESSAGING.md)), its MQTT bridge, streams with their exports ([STREAMS.md](STREAMS.md)) and shadow synchronization ([SHADOWS.md](SHADOWS.md)) keep counters that local IPC (`messaging`, `stream_describe`, `shadows`) can read. A daemon with any of them enabled also summarizes them in each snapshot, so a full queue, a lost broker or a failing export is visible to the fleet and raises alerts ([LOGS.md](LOGS.md#indexer)). Each heartbeat reads the services in effect, whether `config.toml` or a deployment's `services` section ([DEPLOYMENT_SERVICES.md](DEPLOYMENT_SERVICES.md), SF-081) enables them. The bridge's and exporters' counters restart when a deployment reconfigures them. Collecting the summary never changes service state: stream usage and lag leave out messages that retention would retire now, but nothing is deleted until the stream service itself retires it, so a reconfiguration awaiting its commit loses no data to telemetry.

**Versioning.** Such a snapshot has schema 2 and a `services` section with its own `version`. A snapshot without one stays schema 1, byte for byte as before. The indexer, the dApp and the device signing service accept both, and refuse a schema-2 snapshot without a valid section, a schema-1 snapshot with one, an unknown section version and unknown fields.

Section version 2 (SF-084) adds stream positions, and daemons now emit only version 2:

- `oldest` and `newest` on each stream: its first and last retained sequence numbers. Both are `null` exactly when the stream holds no messages.
- `checkpoint` on each export: the last sequence number the exporter acknowledged, or `null` before the first acknowledgement.

The indexer and the dApp accept version 1, which has no positions, and version 2, which requires them. For version 2 they also require `oldest <= newest` and `messages <= newest - oldest + 1`, since sequences skipped by a crash leave gaps. A checkpoint may lie below `oldest - 1`: those messages were lost before export. It may also remain set on an empty stream whose exported messages were discarded.

Upgrade the indexer and the dApp before the daemons: an older indexer rejects schema 2 or section version 2, and the daemon then drops those snapshots. The signed vectors `signed_services` (version 1) and `signed_services_v2` are in [`interfaces/telemetry-cases.json`](../interfaces/telemetry-cases.json).

**What the section holds.** Only counters and states, sections absent (`null`) when a service is not enabled:

| Section | Contents |
|---|---|
| `messaging` | Subscriptions, messages queued, refused publications and dropped QoS 0 messages in total. Per subscriber principal (a component, `@bridge/<n>` or `@operator/<uid>`), fullest first: subscriptions, queued, `fill_percent` (of its fullest subscription, by messages or bytes), `full_since_ms`, refused and dropped |
| `bridge` | The local MQTT broker bridge, when a mapping uses the local broker (an AWS IoT Core-only bridge, [AWS_IOT.md](AWS_IOT.md), is not reported here): `connected`, `since_ms` (when the current state began), connects, inbound messages refused (queue full) and rejected (never acceptable), the last error |
| `streams` | Per stream, most loss first: bytes, `max_size`, `fill_percent`, messages, `oldest`/`newest` retained sequence (version 2), `overwrite`, appends rejected when full. With an export: `checkpoint` (version 2), `lag` (retained messages not exported yet), `lost` (retired before they were exported), batches, failures, `consecutive_failures`, `last_success_ms` and the last error |
| `shadows` | Live shadows and refused notifications. Per synchronized shadow: conflicts settled, failures, `consecutive_failures`, `last_success_ms` and the last error |

Counters that only alert when they grow come with `*_recent`, their increase since the previous snapshot. Pub/sub's refused and dropped counts are persisted, so after a daemon restart their first value is the baseline. The other counters, and `since_ms` / `full_since_ms`, cover the current daemon run. A replaced bridge, or services that were all disabled for a heartbeat and then enabled again, start with fresh counters and condition times.

**Never included:** message payloads, topics or topic filters (a topic can itself be confidential, and nothing marks it so), export and sync URLs, bearer tokens or other credentials, shadow documents and the paths of conflicting keys. Errors are never sent as text: an error text can name a topic filter (a full queue), an inbound MQTT topic (a bridge refusal), a shadow key path or a host. A service's `last_error` is only a category from a fixed vocabulary (`service_error_categories` in [`interfaces/telemetry-cases.json`](../interfaces/telemetry-cases.json): for example `queue-full`, `connection-refused`, `timeout`, `http-server-error`, `unauthorized`, `conflict`, `other`). The daemon keeps the full, redacted text in its log ([LOGS.md](LOGS.md)). The signing service, the indexer and the dApp refuse any other value.

**Bounds.** At most 32 entries per list. Names are at most 128 bytes. If the snapshot would still exceed 64 KiB, components are left out first, then service entries; each list counts what it left out in `omitted`.

**The device's own judgement.** These conditions are degraded reasons. Each threshold is shared with the indexer's alerts:
- a pub/sub queue at least 90 % full for two heartbeat intervals;
- the MQTT bridge disconnected for two heartbeat intervals;
- stream messages retired before export since the previous snapshot;
- a stream export or a shadow sync failing three consecutive times.

One-off refusals, drops and settled shadow conflicts raise alerts, but do not make the device degraded.

## Offline buffering

Snapshots go to a spool under `<state_dir>/telemetry/`, one file per envelope, named by `seq`. Each file is written under a temporary name, synced, then renamed. The spool and its counters survive daemon restarts.

**Upload.** The daemon uploads the spool oldest first, at most 16 envelopes per request, to `POST {url}/v1/telemetry`. The indexer answers each envelope:
- **`accepted` or `duplicate`:** the daemon removes it from the spool.
- **`rejected`:** the daemon logs it and removes it. It is never resent.
- **`retry`:** the daemon keeps it, with everything after it, for the next heartbeat.

If the transport fails or the indexer answers with an error, the daemon keeps everything.

**Full spool.** Beyond `buffer_max` (default 1024, at most 10000), the daemon drops the oldest snapshots. Later snapshots report the total in `dropped`.

**After an outage.** The newest snapshot arrives last, so the indexer's latest report is current again. A report that arrives long after it was taken is never shown as healthy (see `delayed` below).

## Indexer ingress

Start the indexer with a telemetry database:

```bash
suifleet-indexer serve --manifest deployment.json --db fleet.db --tokens tokens \
  --telemetry-db /var/lib/suifleet-indexer/telemetry.db [--telemetry-retain 1000] \
  [--cors-origin https://dapp.example]
```

Without `--telemetry-db`, the telemetry routes answer 404.

**Authentication.** `POST /v1/telemetry` takes no bearer token. Each envelope is authenticated on its own, and is accepted only if all of these hold:
- the signature verifies over the exact bytes. Only ed25519 and secp256r1 signatures are allowed; multisig, zkLogin and passkey signatures are not;
- the snapshot names the address that signed it;
- the Device is indexed, activated, not revoked and not deregistered;
- the signer is the device's **current registry identity signer** (SF-008), as indexed from the chain;
- the snapshot is bound to this chain identifier and registry;
- it is schema 1, or schema 2 with a valid services section, and keeps the bounds above;
- its clock is at most 5 minutes ahead of the indexer's;
- its `seq` is new for the device. The same bytes again are a `duplicate`; other bytes under a used `seq` are `rejected`.

**Retry or reject.** A device that is not indexed yet gets `retry`, because an enrollment can be newer than the index. Every other failure is `rejected`. That includes snapshots signed by a rotated-out key, so they never block a device's spool.

**Limits.** A request may carry at most 16 envelopes and 2 MiB. Put the same TLS reverse proxy in front as for the API, and rate-limit the ingress per client there.

**Storage.** Telemetry goes to its own SQLite file (WAL, full sync), separate from the index:
- It cannot be rebuilt from the chain.
- It is bound to the chain and registry like the index. Another deployment is refused; `--reset` discards its telemetry.
- Per device, only the newest `--telemetry-retain` reports are kept. Each is the signed envelope, its signer, `seq`, the device clock and the time the indexer received it.

## Fleet status

Read routes need a `read` token:

| Request | Returns |
|---|---|
| `GET /v1/health?owner=&group=&cursor=&limit=` | Per device: `status`, `codes`, `reasons`, `last_seen_ms` (when the indexer received the newest report), `observed_ms`, `seq`, `daemon_version`, `components` and the signed `report` itself, plus `server_time_ms` |
| `GET /v1/health/summary?owner=&group=` | `total` and `counts` of `healthy`, `degraded`, `offline` and `unreported` devices |
| `GET /v1/devices/{id}/telemetry?before=SEQ&limit=` | Stored signed reports, newest first |

Only the newest report from the device's **current** signer counts. A report signed by a rotated-out key, or by a revoked identity, is never shown.

**Status rules.** The indexer and the dApp implement these the same way. They are tested on the shared cases in [`interfaces/telemetry-cases.json`](../interfaces/telemetry-cases.json). The interval is the reported one, clamped to 10 to 3600 s.

| Status | Codes | When |
|---|---|---|
| `unreported` | `revoked` | The identity is revoked or the device deregistered: its telemetry is not trusted |
| `unreported` | `no-telemetry` | No report from the current signer |
| `offline` | `offline` (plus any below) | The newest report arrived more than 3 intervals ago |
| `degraded` | `delayed` | The newest report was taken more than 2 intervals before it arrived: it was buffered offline, or the device clock is behind |
| `degraded` | `device-degraded` | The device reports degraded, or lists any reason |
| `degraded` | `inconsistent` | The device says healthy, but a component is not installed, or supervised and not running |
| `healthy` | none | Online, timely, healthy by the device's own judgement, and consistent with its facts |

## dApp

Product views read signed on-chain FleetData summaries and verify current
registry identity and exact device signatures, with chain-cadence freshness.
Services and alerts use the same path. NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL does
not select HTTP health; no read-token UI remains. Configure NEXT_PUBLIC_FLEET_DATA;
see [setup](SERVERLESS_FLEET_DATA.md) and [views](DASHBOARD_VIEWS.md). Customer
integration code may still explicitly read the optional reference HTTP API.

## Verification

| Command | Covers |
|---|---|
| `make edge-test` | Snapshot building, health rules, bounds and truncation (services entries shed after components), the service-health collector (queue fill, persisted and in-memory counter baselines, refusals since the previous snapshot, bridge state periods, errors only as categories (a bridge refusal naming a filter and topic included), no topics or payloads), stream export lag and loss counters, the shared schema-2 vector reproduced byte for byte, the signing service accepting schema 2 only with a services section, the spool (bound, order, durable `seq`, interrupted writes), personal-message signatures checked by Mysten's Rust verifier (ed25519, and P-256 with `--features pkcs11`), config validation, the shared signed vector reproduced byte for byte, and the signing service's telemetry checks (other chain, registry, signer or schema, non-canonical or unknown fields, oversize, transactions; daemon only; a full-size snapshot over the socket) |
| `make indexer-test` | Ingress verification, identity, binding, bounds, clock, replay and retention, the shared vectors (schema 1 and 2) and status cases, services section bounds and versions, unknown service fields refused, the service-health alert rules, store binding and reset, and the HTTP routes: no token on ingress, tokens on reads, batch limit, CORS, telemetry disabled |
| `node scripts/test_telemetry.cjs` (in `make test-manifest`) | The dApp verifier on the shared vectors, with schema-2 services sections refused when they carry unknown (a topic, an export URL), missing or oversized fields and on tampered, foreign, multisig and P-256 reports; the binding checks; the shared status cases; identity reads; paginated fleet health with forged, revoked, unactivated and unreported devices; an unavailable service; a chain identity outage, a failed refresh and a stalled refresh after a healthy result, with the real TanStack Query cache |
| `python3 scripts/test_telemetry.py` (in `make test-ota`) | The real daemon against the chain simulator and a stand-in ingress: bounded offline spooling with drops counted, the spool across a restart, `retry` resent in order, `rejected` dropped, a running supervised component reported healthy, a crash past the restart budget reported degraded, the daemon signing through the real signing service (development caller rule) while arbitrary and non-canonical messages are refused, one compact on-chain receipt, and every envelope verified by the dApp's verifier |
| `CHROME_EXECUTABLE=… node scripts/test_health_browser.cjs` | Direct-chain product acceptance (same suite as test_dashboard_browser.cjs): Devices health without a token, browser-derived dashboard/service alerts, forged report refusal, rollouts and archive role/search/session checks. Fullnode and wallet are fixtures; no HTTP indexer URL |
| `make test-service-health` | The real daemon as root with real component UIDs, the chain simulator, a stand-in ingress, a minimal MQTT broker, an export destination and a shadow service, without network. A healthy baseline raises nothing. A subscriber's QoS 1 queue fills and a publication is refused; the broker stops; the export answers 503 while an overwrite stream retires unexported messages; the shadow service is down, then a key changed on both sides is settled. The real indexer (`suifleet-indexer telemetry-alerts`) derives all eight service alerts from the signed reports, and the sustained ones degrade the device. After recovery the alerts resolve and the device is healthy. No snapshot contains a payload, topic, export path, token or shadow value, and the dApp verifies them all |
| `make test-indexer-localnet` | Real daemons against a real localnet and the real indexer. The activated device is served healthy, and its stored reports are verified again by the dApp's verifier. The revoked device's snapshots are rejected. Telemetry survives an indexer restart |

**Not yet covered:**
- service health against a real Mosquitto broker inside the telemetry test (the bridge's Mosquitto interoperability is `make test-messaging`), and with a hosted indexer and webhook;
- the dashboard with a real extension wallet and live chain reads against a hosted indexer (#32);
- public-network operation (#11);
- physical Pi heartbeat and power loss (#32).

The indexer has no packaging or hosted instance yet ([INDEXER.md](INDEXER.md)).
