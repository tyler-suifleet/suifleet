# Product operation without application servers (SF-137)

SuiFleet operates no application server, indexer, health ingress, notification
relay or retention worker. The product is a static wallet dApp plus devices,
Sui and Walrus. Fullnodes, Walrus storage/aggregators and Seal key servers are
network dependencies; this is not a claim that those protocols are offline.
Customers may integrate their own pipelines (SF-143); SuiFleet hosts none.

## Dependency inventory and replacements

| Former dependency / entry points | Product replacement / ownership |
|---|---|
| Daemon HTTP heartbeat, inventory and health: telemetry.rs, config.rs, linux_main.rs; TELEMETRY.md | [fleet_data] publishes signed, bounded 16 KiB health to Sui. Chain enforces cadence and identity. Owner enrolls the writer and funds the GasAccount first; omission is not implicit enrollment or permission to spend. |
| Messaging queues/loss, MQTT bridge, streams/ranges/checkpoints/export loss, shadow sync: service_health.rs, fleet_data.rs; MESSAGING.md, STREAMS.md, SHADOWS.md, DEPLOYMENT_SERVICES.md | The same services v2 summary is collected by the on-chain producer, with bounded entries/omission counts, categorical errors and no payloads, topics or credentials. Browser verifies signature and current identity. Shadow documents remain encrypted in archives. |
| HTTP logs/diagnostics: logs.rs, fleet_archive.rs; LOGS.md | [logs] collects/redacts locally; [fleet_data.archives] encrypts to prepaid certified Walrus blobs and on-chain anchors. /data requests diagnostics on chain; device processes them at configured cadence. |
| Health UI: useFleetHealth, telemetry.ts, Devices, Dashboard and Services | Direct-chain fetchChainHealth. No telemetry URL/read token. Missing configuration, invalid signature, revoked identity, stale data and read failures stay explicit unavailable/unreported states. |
| Alerts: useFleetAlerts, alerts.ts, Dashboard, Services | fetchChainAlerts reads health and authoritative deployment receipts; browser derives service, offline, degraded, broken, pending and failed-deployment incidents. Updates require the dApp open. |
| Log search / diagnostics / support reads | /data discovers on-chain anchors, decrypts verified Walrus archives in memory using owner/support Seal sessions and offers bounded literal search. Session access records are ephemeral, not durable read audits (DASHBOARD_VIEWS.md). |
| Staged rollout health gate | Deployment-bound signed post-apply health and receipts on Sui, relative to admission grants. It does not read periodic fleet health or an HTTP indexer; see [STAGED_ROLLOUTS.md](STAGED_ROLLOUTS.md). |
| Devices, groups, deployment history/receipts, rollouts and recovery | Verified gRPC readers, event discovery and local transactions; old package/status fields remain untrusted. fleet-recovery.ts exports/discovers directly, without server fallback. |
| Operations CLI / API-built bulk transactions: suifleet-indexer ctl, txbuild.rs | scripts/suifleet_ops.cjs reads chain and builds unsigned transactions locally with dApp readers/builders. No API/token/key. Borrow/add/return preserves custody; sender/admin/identity prechecks precede construction. Wallet signs separately; contracts retain authority. |
| fleet_alert_relay.cjs / indexer webhooks | Optional customer-run notification integration. Browser alerts need neither. Customers own webhook availability, credentials and scheduling. |
| retention_worker.py / retention/renewal scheduling | Optional customer-run automation. dApp retention/renewal actions and prepaid policies are the product path (RETENTION_RENEWAL.md). No hosted scheduler or automatic renewal guarantee merely from funding. |
| CloudWatch metrics/log export companions / cloudwatch_telemetry.py | Optional customer/device-owned AWS integrations. Remote snapshots require exact signature/current on-chain identity verification; trusted direct-chain readers can supply them. Customer owns AWS accounts/policies/credentials. |
| suifleet-indexer/, HTTP API, change feed, SQLite support audit, legacy ctl | Optional customer-hosted reference integration, not installed/started by product packaging. Customers requiring a pipeline/change feed or durable HTTP support audit operate it. No serverless chain-read audit is claimed. Explicit HTTP library readers remain for this reference; product hooks/environment never select them. |
| Historical indexer test tools (test_indexer_localnet.py, test_telemetry.py, test_logs.py, service-health fixtures) | Reference-integration regression tests; their HTTP fixtures do not establish serverless acceptance. See serverless checks below. |

No committed manifest configures a SuiFleet-hosted indexer. The legacy
NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL no longer selects a product backend, even if
left in a build environment. Configure NEXT_PUBLIC_FLEET_DATA as described in
[serverless setup](SERVERLESS_FLEET_DATA.md).

## Device configuration and migration

Use owner-created/enrolled FleetData, DeviceData, FleetBudget, GasAccount and
FleetStorage IDs, actual per-package origins, public Seal provider metadata and
reviewed codec digest. See config.example.toml and SERVERLESS_FLEET_DATA.md.
Preserve keys, journals, logs, writer sequences and pending archive transactions.
Never replace a writer to reset cadence.

Remove [telemetry] to stop HTTP output. [logs] remains archive collection
configuration. [telemetry] and [fleet_data] are mutually exclusive; startup
rejects configuring both. Their signed health schema is shared, with independent
sequence counters, so concurrent producers would permit cross-transport replay.
Customer HTTP health is an explicit alternative, not a parallel product backend.
Exclusivity does not add a cryptographic transport domain: a customer HTTP
receiver must not trust an identity whose signed health has been published on
chain, including historical reports. Retire that receiver authorization when
migrating to on-chain health; do not reuse an on-chain health identity for HTTP.
When archives are enabled they exclusively own the log cursor: no parallel HTTP
log shipper starts. An HTTP-only customer integration can use [logs] with
[telemetry]. Previously buffered HTTP data stays on disk, never silently
reclassified or claimed uploaded to Walrus.

## Chain operations CLI

Install pinned dApp dependencies first (make dapp-install). The CLI shares the
reviewed TypeScript modules and SDK through the existing Node module loader;
it needs no Rust reference indexer. Commands:

    node scripts/suifleet_ops.cjs deployment.json devices 0xOWNER
    node scripts/suifleet_ops.cjs deployment.json groups 0xOWNER
    node scripts/suifleet_ops.cjs deployment.json deployments 0xOWNER
    node scripts/suifleet_ops.cjs deployment.json health 0xOWNER
    node scripts/suifleet_ops.cjs deployment.json alerts 0xOWNER
    node scripts/suifleet_ops.cjs deployment.json build request.json

Health/alerts require NEXT_PUBLIC_FLEET_DATA with complete verified configuration.
A schematic build request (replace placeholders):

    {"command":"add-devices","sender":"0xOWNER","group":"0xGROUP","devices":["0xDEVICE"],"out":"unsigned.json","maxGasMist":50000000}

remove-devices uses the same shape. create-deployment uses component, version,
recipeBlob, artifactSha256, recipeSha256, targetGroups and targetDevices. Digests
must be 64 lowercase hex digits. CLI verifies chain identity, latest package and
UpgradeCap metadata, registry and type origins, then reads current ownership and
membership and constructs intent locally. It resolves references/gas through
the fullnode, writes a new unsigned file (never overwrites), and never signs or
submits. Explicit gas ceiling is at most 0.5 SUI. Review/sign in your wallet and
inspect execution effects separately. Encrypted log search and diagnostic
requests use /data; the legacy reference support-token CLI is customer-owned.

## Validation boundary

Required evidence: pinned frontend typecheck/production export, signed reader
and operations regressions, real browser pages without an indexer URL, daemon
unit tests/serverless OTA simulator and isolated localnet direct-chain
operations/health with no application HTTP server. Fullnodes/faucet are test
infrastructure. Walrus/Seal fixtures are not production provider acceptance.
Exact run results belong in PR/issue; commands here do not claim checks ran.

Final provider/manual-wallet, physical and Mainnet acceptance stays unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
