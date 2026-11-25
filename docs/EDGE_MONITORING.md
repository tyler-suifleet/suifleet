# Customer-owned Prometheus and OpenTelemetry monitoring (SF-303)

`scripts/edge_monitoring.py` is an opt-in operational monitoring adapter for one
edge device. It reads the daemon's existing health, inventory, deployment
journal and bounded component log tails from the
[local support console](LOCAL_CONSOLE.md) and exports them to monitoring the
customer owns:

- **Prometheus (local pull):** a text-format `/metrics` endpoint, loopback by
  default and bearer-protected, scraped by the customer's Prometheus.
- **OpenTelemetry (direct push):** OTLP/HTTP JSON metrics and redacted log
  records sent to the customer's collector or OTLP-compatible backend.

There is no SuiFleet server in this path, no per-metric chain transaction and
no device key use. The adapter is **disabled unless its configuration says
`"enabled": true`**; nothing installs or starts it automatically. It does not
replace the authoritative on-chain deployment receipts, signed
[fleet health](TELEMETRY.md) or complete [log shipping](LOGS.md); it is a
monitoring view of the same local state.

External acceptance remains separate and unrun: physical targets in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32); hosted or
paid monitoring providers, production credentials and mainnet in
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Platform scripts keep owning reboot, firmware installation and boot recovery;
the adapter only observes.

## How it reads the device

The adapter calls `POST /api/v1/request` on the console with a bearer token,
exactly as the console's CLI clients do. Give it a dedicated console user with
read grants only:

```toml
[console]
listen = "127.0.0.1:1441"

[console.users.monitoring]
token_sha256 = "<from suifleet --console-token>"
grants = ["inventory", "health", "deployments", "logs"]   # drop "logs" if not exporting logs
```

Never grant `configuration`, `audit` or a support operation to the monitoring
user. Each poll makes one `health` call, paged `inventory` calls (at most 256
components) and paged `deployments` calls (at most 10 pages of 100 journal
entries); with logs enabled, one `logs` call per listed component. All of
these are read views served from the daemon's state files; a poll never
changes device state and never waits for the poll loop. Console responses are
bounded at 64 KiB by the daemon and 70 KiB by the adapter.

The console's own [trust boundary](LOCAL_CONSOLE.md#trust-boundary) applies:
run the adapter as its own Unix user, not as the daemon user or a component
user. Each console request is audited by the daemon.

## Configuration

Generate a token for Prometheus to present, and keep only its hash in the
adapter configuration:

```sh
python3 /opt/suifleet/scripts/edge_monitoring.py --token
# {"token": "sfm_…", "token_sha256": "…"}
```

Example `/etc/suifleet-monitoring/monitoring.json` (replace example values):

```json
{
  "enabled": true,
  "evidence": "physical",
  "device": "line-3-gateway",
  "interval_seconds": 60,
  "console": {
    "url": "http://127.0.0.1:1441",
    "token_file": "/etc/suifleet-monitoring/console.token"
  },
  "prometheus": {
    "listen": "127.0.0.1:9469",
    "token_sha256": "<token_sha256 from --token>"
  },
  "otlp": {
    "endpoint": "https://otel-collector.example.internal:4318",
    "headers_file": "/etc/suifleet-monitoring/otlp.headers",
    "timeout_seconds": 5,
    "initial_backoff_seconds": 5,
    "max_backoff_seconds": 300
  },
  "logs": {
    "components": ["sensor-reader"],
    "max_lines": 50,
    "max_buffer": 1000
  }
}
```

| Field | Rule |
|---|---|
| `enabled` | Default `false`. When false or absent, the adapter prints that it is disabled and exits 0 without reading token files, polling or listening. |
| `evidence` | Required when enabled: `simulated`, `physical` or `unclassified`. Attached to every metric and to the OTLP resource. It is an operator declaration so test rigs and real devices never share dashboards by accident; it is not hardware attestation. SuiFleet's own tests only produce `simulated`. |
| `device` | Label, 1–128 characters of `[A-Za-z0-9._:-]`. Optional for Prometheus-only use; **required with `otlp`**, where it is part of the OTLP resource identity and must be stable and unique among devices sharing the collector (otherwise their points would land in one series). Choose a non-secret name; it is not the Sui Device object ID unless you put it there. |
| `interval_seconds` | Console poll and OTLP export cadence, 10–3600, default 60. Pick it with the daemon's poll and heartbeat cadence in mind; scraping `/metrics` more often only returns the last poll. |
| `console.url` | `http://<loopback address>:<port>`; the console is loopback-only. |
| `console.token_file` | One `sfc_…` token from `suifleet --console-token`. |
| `prometheus.listen` | `host:port`, default `127.0.0.1:9469`. A non-loopback address needs `"allow_remote": true` **and** `token_sha256`. The endpoint speaks plain HTTP: for scraping across a network, put a customer-owned TLS proxy in front, or scrape over SSH/WireGuard. |
| `prometheus.token_sha256` | Optional for loopback, required otherwise. Requests must carry `Authorization: Bearer <token>`; only the hash is stored. |
| `otlp.endpoint` | Collector base URL; the adapter posts to `/v1/metrics` and `/v1/logs`. HTTPS is required unless the collector is on loopback. No credentials, query or fragment in the URL. |
| `otlp.headers_file` | Optional `Name: value` lines (at most 16), for example `Authorization: Bearer …` or a tenant header. Host, Content-Type, Content-Length, Connection and Transfer-Encoding cannot be overridden. |
| `otlp.ca_file` | Optional PEM bundle for a private CA (HTTPS only). Otherwise the system trust store is used. TLS 1.2+ is required; session-key logging is disabled. |
| `otlp.timeout_seconds` | 1–30, default 5, per request. |
| `otlp.initial_backoff_seconds`, `otlp.max_backoff_seconds` | Retry delay after a failure, doubling up to the maximum (defaults 5 and 300). |
| `otlp.logs` | Default `true`; set `false` to send metrics only. |
| `logs.components` | 1–16 supervised component names whose log tails are exported (OTLP only; Prometheus does not ingest logs). |
| `logs.max_lines` | Lines requested per component per poll, 1–200 (the console's limit), default 50. |
| `logs.max_buffer` | Redacted lines held for export while the collector is unavailable, 10–10000, default 1000. |

Unknown fields and out-of-range values are rejected before anything starts.
Validate with `--check-config`. The configuration file must be a regular file,
not a symlink, owned by the adapter user or root and not writable by group or
others. Token, header and CA files must be absolute; token and header files
must be private (mode 0600). Error messages never include file contents.

Run it under the platform service manager as a dedicated user, for example with
systemd `User=suifleet-monitoring`, `NoNewPrivileges=yes`, `ProtectSystem=strict`,
`PrivateTmp=yes`, `UMask=0077`, `MemoryMax=64M` and `Restart=on-failure`:

```sh
python3 /opt/suifleet/scripts/edge_monitoring.py --config /etc/suifleet-monitoring/monitoring.json
```

Python 3.10+ with only the standard library is required. Changing the
configuration or rotating a token or header file requires a restart.

## Metrics

Every series carries `evidence` and, when configured, `device`. Metric help
texts start with **Current:** or **Historical:** so dashboards and alerts keep
the two apart.

| Metric | Kind | Meaning |
|---|---|---|
| `suifleet_up` | gauge | Current: 1 when the last console read succeeded. While 0, every device metric below is withdrawn instead of repeating stale values. |
| `suifleet_device_healthy` | gauge | Current: the daemon's own judgement, 1 healthy / 0 degraded. |
| `suifleet_device_health_reasons` | gauge | Current: number of degraded reasons. Reason texts are not exported. |
| `suifleet_device_uptime_seconds` | gauge | Current: daemon uptime. |
| `suifleet_device_pending_receipts` | gauge | Current: outcomes not yet reported on chain. |
| `suifleet_device_sync_consecutive_failures` | gauge | Current: failed chain synchronizations in a row. |
| `suifleet_device_sync_last_success_timestamp_seconds` | gauge | Current: last successful chain synchronization. |
| `suifleet_daemon_info{version}` | gauge | Daemon version. |
| `suifleet_component_installed{component}` | gauge | Current: install confirmed. |
| `suifleet_component_running{component}` | gauge | Current: supervised component running (supervised components only). |
| `suifleet_component_phase{component,phase}` | gauge | Current lifecycle phase: `starting`, `running`, `backoff`, `stopping`, `stopped`, `exited`, `broken`, `installed`, `unconfirmed` or `other`. |
| `suifleet_component_restarts{component}` | gauge | Restarts in the current daemon run; resets when the daemon restarts. |
| `suifleet_components_truncated` | gauge | 1 when more than 256 components exist. |
| `suifleet_deployment_outcomes{outcome}` | gauge | Historical: journal entries by `applied`, `failed`, `indeterminate` or `pending`. Past failures stay visible while the device is currently healthy. |
| `suifleet_deployment_outcomes_unreported` | gauge | Historical: terminal outcomes whose receipt is not on chain yet. |
| `suifleet_deployment_outcomes_truncated` | gauge | 1 when the journal has more than 1000 entries and counts are partial. |
| `suifleet_exporter_info{adapter_version}` | gauge | Adapter version. |
| `suifleet_exporter_polls_total{result}` | counter | Console reads by result (`ok`, `unreachable`, `unauthenticated`, `denied`, `unavailable`, `malformed`, and `logs-…` for log reads). |
| `suifleet_exporter_last_success_timestamp_seconds` | gauge | Last successful console read. |
| `suifleet_exporter_otlp_exports_total{signal,result}` | counter | OTLP posts by `metrics`/`logs` and `ok`, `partial` (accepted with 2xx, but the collector's partial-success reply refused a positive number of data points or log records; not resent), `invalid_response` (accepted with 2xx, but the reply could not be inspected: larger than 64 KiB, not a JSON object, a partial-success count that is not a nonnegative int64, or a count larger than the data points or log records sent; the batch is not resent and its log lines count as `unconfirmed`, not `exported`), `rejected`, `retry`, `unauthorized` or `unreachable`. |
| `suifleet_exporter_otlp_metric_points_rejected_total` | counter | Metric data points refused in OTLP partial-success replies (`rejectedDataPoints`). |
| `suifleet_exporter_otlp_backoff_seconds{signal}` | gauge | Current retry delay; 0 when healthy. |
| `suifleet_exporter_log_records_total{result}` | counter | Log lines `exported`, `dropped` from the full buffer, `rejected` by the collector, or `unconfirmed` (sent and accepted with 2xx, but the reply could not be inspected for partial rejection). |
| `suifleet_exporter_log_gaps_total` | counter | Polls whose tail did not overlap the previous one, so lines may have been missed. |
| `suifleet_exporter_log_buffered` | gauge | Lines waiting for export. |

Never exported: degraded-reason texts, deployment error texts, last-exit texts,
configuration, audit records, tokens, header values, Sui keys or addresses.
Component names and the daemon version are exported as labels.

OTLP metrics use the same names. Gauges become OTLP gauges; counters become
cumulative monotonic sums starting at adapter start. The resource carries
`service.name=suifleet-edge`, `suifleet.evidence`, `suifleet.device` and
`suifleet.monitoring.adapter_version`. These stay the same while the console is
down and across daemon upgrades, so a `suifleet_up` 0 point lands on the same
series as earlier points. The daemon version is reported only as the
`suifleet_daemon_info{version}` data point.

## Logs

With `logs` configured, each poll reads the last `max_lines` lines of each
listed component through the console, which has already redacted them
([LOCAL_CONSOLE.md](LOCAL_CONSOLE.md#protocol-v1)). The adapter redacts each
line again as defense in depth: secret-looking `name=value` pairs, Bearer/Basic
credentials, PEM markers and bare base64 lines, `suiprivkey…` keys, console and
monitoring tokens, AWS access key IDs, JWT-like values and bare hex of 64+
characters. Over-redaction is preferred to a leak; components must still not
log secrets.

New lines are found by overlapping the new tail with the previous one. This is
bounded **tail sampling**, not complete log shipping:

- a component that writes more than `max_lines` lines between polls loses the
  excess, counted in `suifleet_exporter_log_gaps_total`;
- consecutive identical lines can be collapsed by the overlap;
- lines are timestamped when the adapter reads them;
- after an adapter restart the current tail is sent again.

For complete, durable log delivery use [SF-020 logs](LOGS.md) or
[CloudWatch Logs](CLOUDWATCH_LOGS.md).

## Failures and backoff

- **Console unavailable** (daemon stopped, token revoked, grant missing):
  `suifleet_up` becomes 0, device metrics are withdrawn and the poll result is
  counted. Prometheus still scrapes the adapter, so an alert on
  `suifleet_up == 0` (and on `up == 0` for the adapter itself) covers both.
- **Collector unavailable, 5xx, 429 or 401/403:** the post is retried after
  `initial_backoff_seconds`, doubling per consecutive failure up to
  `max_backoff_seconds`, with ±20 % jitter. A `Retry-After` header raises the
  delay up to the maximum. Metrics and logs back off independently. Metrics
  are not queued: the next successful post carries current values (counters
  are cumulative). Log lines stay buffered up to `max_buffer`; when it is full
  the oldest lines are dropped and counted.
- **Collector rejects a payload** (400, 413, 422): it is not resent and is
  counted as `rejected`. An OTLP partial-success reply with a positive count is
  recorded as `partial`, not `ok`, and is not resent: refused log records count
  as `rejected` and refused metric data points add to
  `suifleet_exporter_otlp_metric_points_rejected_total`. A count larger than
  the number sent is impossible and is recorded as `invalid_response` instead.
- **Wrong collector credential:** reported as `unauthorized` and retried with
  backoff; fix the header file and restart.
- Requests use no proxy environment and follow no redirects. Request and
  response bodies, headers and tokens are never logged.

Delivery is best effort. A crash loses buffered log lines; the adapter keeps
no on-disk spool.

## Resource bounds

One poll/export loop and one single-threaded `/metrics` server (5 s per
request, listen backlog 16) run in the adapter. Memory is bounded by the
component limit, the log buffer (`max_buffer` lines of at most 512 bytes) and
at most 512 log records per OTLP request. The end-to-end test asserts the
adapter's peak RSS stays below 64 MiB. Size the customer's own Prometheus
retention and collector `memory_limiter` for their fleet.

## Prometheus and OpenTelemetry Collector examples

Prometheus scrape job (on the device, or across a customer-owned TLS proxy):

```yaml
scrape_configs:
  - job_name: suifleet-edge
    scrape_interval: 60s
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/suifleet-edge.token
    static_configs:
      - targets: ["127.0.0.1:9469"]
```

Example alert rules:

```yaml
groups:
  - name: suifleet-edge
    rules:
      - alert: SuiFleetEdgeUnavailable
        expr: suifleet_up == 0 or up{job="suifleet-edge"} == 0
        for: 5m
      - alert: SuiFleetEdgeDegraded
        expr: suifleet_device_healthy{evidence="physical"} == 0
        for: 10m
      - alert: SuiFleetDeploymentFailed
        expr: increase(suifleet_deployment_outcomes{outcome="failed"}[1h]) > 0
```

OpenTelemetry Collector (core distribution) receiving the adapter's pushes:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
        tls:
          cert_file: /etc/otelcol/tls.crt
          key_file: /etc/otelcol/tls.key
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 256
  batch: {}
exporters:
  debug: {}           # replace with the customer's backend exporter
service:
  pipelines:
    metrics: {receivers: [otlp], processors: [memory_limiter, batch], exporters: [debug]}
    logs:    {receivers: [otlp], processors: [memory_limiter, batch], exporters: [debug]}
```

Authenticate the adapter at the customer's collector or gateway (for example a
contrib-distribution bearer-token authenticator or an authenticating TLS
proxy), and put the matching `Authorization` header in `otlp.headers_file`.

## Verification

```sh
make test-edge-monitoring SUIFLEET_DEV_IMAGE=suifleet-dev:ci
```

`scripts/test_edge_monitoring.sh` fetches Prometheus v3.5.0 and OpenTelemetry
Collector core (image tag 0.140.0; the binary reports 0.140.1) by multi-architecture image digest, copies their
binaries out, builds the daemon, then runs everything in a network-none
container:

- `scripts/tests/test_edge_monitoring.py` (offline unit tests): configuration
  and disabled-by-default behavior, file protections, redaction vectors,
  current/historical/evidence metrics, console failures, `/metrics`
  authentication, OTLP payloads, Retry-After and capped exponential backoff,
  rejected and partial-success payloads, unreachable collectors, the bounded
  log buffer and gap counting, and the run loop.
- `scripts/test_edge_monitoring.py` (end to end): the real daemon against the
  local chain/Walrus simulator with its console; one failed and one applied
  deployment; the real adapter process; the real Prometheus scraping it with
  a bearer token file (and a wrong token refused with 401); the real collector
  receiving OTLP metrics and redacted logs through an authenticating gateway
  (and a wrong credential refused); a collector outage with backoff and
  gap-free delivery of buffered lines after recovery; a stopped daemon
  reported as `suifleet_up 0`; no token, credential or log canary in any
  export; and peak RSS of the adapter, Prometheus and collector.

This establishes simulated evidence only. Physical Pi/target monitoring,
customer-hosted or paid monitoring services, production TLS/credentials and
mainnet remain unrun ([SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32),
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159)). No Move
layout, daemon configuration or client migration is involved.
