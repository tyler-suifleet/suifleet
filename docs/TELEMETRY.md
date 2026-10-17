# Device heartbeat, inventory and health (SF-019)

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
- it is schema 1 and keeps the bounds above;
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

Set `NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL` to the indexer's base URL and rebuild. Start the indexer with `--cors-origin` set to the dApp's origin. The operator enters a `read` token on the Devices page. It is kept for that browser tab only (`sessionStorage`), never in the build.

**What the dApp checks.** The dApp does not take the indexer's word for health:
- It reads each activated device's identity (signer and revocation) from the registry on chain.
- It verifies the newest report's signature against that signer with Mysten's SDK. Only ed25519 and P-256 signature flags are allowed.
- It checks that the report is bound to this manifest's chain and registry, and to the device.
- It then applies the status rules itself.

A report that fails any check is shown as **Unverified**. A compromised indexer can withhold or delay reports, or misstate when it received them, which decides online versus offline. It cannot invent a device's health.

**Display.** The Devices page shows the status, the last report's age (by the indexer's clock) and the reasons. Expanding a device shows its installed components, versions, runtime phases, restarts and last exits. The dashboard shows how many devices are healthy.

If no telemetry URL is configured, or the service is unreachable or refuses the token, health is shown as unavailable. Unactivated devices cannot sign telemetry. A device whose chain identity cannot be read, or whose report cannot be checked, is unavailable on its own. The dApp refreshes health every 15 s. If a refresh fails, or no refresh has succeeded for 30 s, every device is shown as unavailable instead of its earlier result, so an indexer or chain outage never freezes an earlier healthy status.

## Verification

| Command | Covers |
|---|---|
| `make edge-test` | Snapshot building, health rules, bounds and truncation, the spool (bound, order, durable `seq`, interrupted writes), personal-message signatures checked by Mysten's Rust verifier (ed25519, and P-256 with `--features pkcs11`), config validation, the shared signed vector reproduced byte for byte, and the signing service's telemetry checks (other chain, registry, signer or schema, non-canonical or unknown fields, oversize, transactions; daemon only; a full-size snapshot over the socket) |
| `make indexer-test` | Ingress verification, identity, binding, bounds, clock, replay and retention, the shared vector and status cases, store binding and reset, and the HTTP routes: no token on ingress, tokens on reads, batch limit, CORS, telemetry disabled |
| `node scripts/test_telemetry.cjs` (in `make test-manifest`) | The dApp verifier on the shared vector and on tampered, foreign, multisig and P-256 reports; the binding checks; the shared status cases; identity reads; paginated fleet health with forged, revoked, unactivated and unreported devices; an unavailable service; a chain identity outage, a failed refresh and a stalled refresh after a healthy result, with the real TanStack Query cache |
| `python3 scripts/test_telemetry.py` (in `make test-ota`) | The real daemon against the chain simulator and a stand-in ingress: bounded offline spooling with drops counted, the spool across a restart, `retry` resent in order, `rejected` dropped, a running supervised component reported healthy, a crash past the restart budget reported degraded, the daemon signing through the real signing service (development caller rule) while arbitrary and non-canonical messages are refused, one compact on-chain receipt, and every envelope verified by the dApp's verifier |
| `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_health_browser.cjs` | The actual Devices page in a real browser, against a real CORS-enabled HTTP health service serving reports signed with Mysten's SDK. Without a token it shows health unavailable. With the token it shows Healthy, Degraded, Offline, Unverified (forged) and Unreported per device, verified against the chain identities, plus not activated, last seen, reasons and installed components. A forged report's inventory is hidden, and forgetting the token claims nothing again. The wallet and chain reads are fixtures |
| `make test-indexer-localnet` | Real daemons against a real localnet and the real indexer. The activated device is served healthy, and its stored reports are verified again by the dApp's verifier. The revoked device's snapshots are rejected. Telemetry survives an indexer restart |

**Not yet covered:**
- the dashboard with a real extension wallet and live chain reads against a hosted indexer (#32);
- public-network operation (#11);
- physical Pi heartbeat and power loss (#32).

The indexer has no packaging or hosted instance yet ([INDEXER.md](INDEXER.md)).
