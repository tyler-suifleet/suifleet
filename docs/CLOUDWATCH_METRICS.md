# Scoped CloudWatch metrics export

SF-059 provides an opt-in Linux companion, `scripts/cloudwatch_metrics.py`, pinned
to the request, units, value bounds and batching semantics of
[`aws.greengrass.Cloudwatch` 4.0.0](https://docs.aws.amazon.com/greengrass/v2/developerguide/cloudwatch-metrics-component.html)
(reference checked 2026-10-19). It accepts component and device measurements;
collectors explicitly publish them. An explicit SF-019 snapshot sender is described below. It does not automatically forward Sui telemetry,
translate AWS recipes or load AWS component artifacts.

The adapter sends the CloudWatch Query API `PutMetricData`, version `2010-08-01`,
signed with SigV4 for `monitoring`. Production endpoints are derived from an explicit
AWS region; no endpoint override, proxy environment, static credentials, profile or
instance metadata provider is used. China/isolated AWS partitions are not implemented.
TLS uses an explicit verifying context and ignores SSLKEYLOGFILE session-key logging.
Actual AWS delivery remains **unrun and unauthorized**, tracked in
[SF-059/#104](https://github.com/tyler-suifleet/suifleet/issues/104).
Physical target acceptance remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).

## Provisioning and invocation

Install `cloudwatch_metrics.py` and `aws_credentials.py` together as trusted,
read-only code, plus `sdk/python/suifleet_aws.py` at the corresponding repository
relative path. Python 3.10+, Linux SO_PEERCRED, SQLite and libacl are required.
Run as a dedicated platform-managed adapter UID, separate from daemon and publishers.
Use the platform service manager and the hardening guidance in
[AWS_CREDENTIALS.md](AWS_CREDENTIALS.md); set UMask=0077, LimitCORE=0,
NoNewPrivileges=yes, Restart=on-failure and writable paths only for runtime/state.
No package, account, IAM role or system service is installed automatically.

Grant only this adapter UID/component through the SF-057 credential service.
Its AWS role must allow only `cloudwatch:PutMetricData`, resource `*`, with an
operator-selected `cloudwatch:namespace` condition. A local publisher grant does
not grant access to credentials. The adapter obtains the per-start container
credential endpoint/token from SF-057; AWS credentials refresh on each outgoing
request and remain in memory only. Restart the service before the SF-057 per-start
token TTL expires. Neither Sui device keys nor the Sui signer are used.

Example protected JSON configuration (replace all example identities):

```json
{
  "socket": "/run/suifleet-metrics/metrics.sock",
  "queue": "/var/lib/suifleet-metrics/queue.sqlite",
  "core_name": "sensor-1",
  "region": "us-east-1",
  "credential_socket": "/run/suifleet-aws/credentials.sock",
  "credential_component": "metrics-export",
  "daemon_uid": 1001,
  "publishers": [2001, 2002],
  "interval": 10,
  "retain": 5000
}
```

Configuration must be absolute, service-owned and mode 0600, with protected
root/service-owned ancestors and no symlinks. Preprovision the socket directory
service-owned, mode 0755, and the queue directory service-owned, mode 0700.
Each directory needs an empty service-owned mode-0600 `.owner` marker; ancestors
must be non-writable to group/others. Keep the queue directory outside component
workspaces. The socket receives ACL access only for configured publisher UIDs;
all others have no access. Kernel UID checks still reject unauthorized callers
if filesystem permissions are accidentally widened. Publisher UIDs must be
positive, distinct from the adapter and declared daemon UID, and distinct per
component. Sharing a UID shares authority. Config/grant changes require restart;
previously accepted queued measurements remain eligible for delivery.

```sh
python3 /opt/suifleet/scripts/cloudwatch_metrics.py --config /etc/suifleet-metrics.json
```

Publishers send one newline-terminated UTF-8 JSON message per Unix connection,
at most 16 KiB including newline, and receive one JSON response. The topic
contract is `cloudwatch/metric/put`; this dedicated socket is its explicit ingress,
not an implicit subscription to every native IPC topic. No client-supplied
publisher name/UID grants authority. A component or device collector can send:

```json
{"request":{"namespace":"Fleet","metricData":{"metricName":"CPU","value":12.5,"unit":"Percent","dimensions":[{"name":"component","value":"sensor"}],"timestamp":1792400000}}}
```

The adapter adds `coreName`. Units, dimensions and numeric validation follow the
[pinned contract](GREENGRASS_MANAGED_COMPONENTS.md#cloudwatch-metrics-export-contract).
Omitted timestamps are captured at durable enqueue time; supplied Unix seconds
are converted to UTC, with unrepresentable dates rejected. The remote service may
reject timestamps outside its own acceptance window. Invalid schema/frame gives
`InvalidInputException`; unauthorized kernel UID gives `Unauthorized`. Accepted
records receive `success`, `buffered`, `dropped`, `rejected`. These counters describe
local acceptance/delivery, not CloudWatch ingestion verification.

## Native device and component telemetry

`scripts/cloudwatch_telemetry.py` maps an explicitly supplied **trusted** SF-019
schema-1 snapshot to the same socket. Run it as a dedicated granted publisher UID,
with read access only to an operator-exported snapshot. Do not grant shared daemon
UID access, expose the daemon state directory, or feed unverified remote messages.
The sender performs schema/counter bounds checks, not signature authentication;
remote exports must first pass the indexer signature/identity verification.

```sh
python3 /opt/suifleet/scripts/cloudwatch_telemetry.py \
  --snapshot /run/metrics-input/snapshot.json --namespace Fleet \
  --socket /run/suifleet-metrics/metrics.sock
```

The numeric allowlist is `Healthy`, `Uptime` (Seconds), `TelemetryDropped`,
`PendingReceipts`, `SyncFailures`, and per-component `Installed`/`Restarts` (Count).
Each uses the snapshot observation timestamp; component records carry the
`component` dimension and the adapter adds `coreName`. Error/exit/reason text,
signatures, registry and signer IDs are omitted. This is a SuiFleet mapping, not
a claim that NucleusEmitter produces identical names. The sender exports once;
the platform chooses collection/scheduling. A failure midway can leave a partial
set queued, so retry can duplicate values. The existing SF-019 signed fixture is
executed through this mapping and the actual adapter into observed HTTP requests.

## Durability, bounds and failure behavior

`interval` is 0–900 seconds; `retain` is 2000–10000 records (5000 default).
The upper bound is an explicit SuiFleet resource limit. SQLite FULL synchronous
transactions persist enqueue, removals and drop/rejection counters. One file lock
prevents concurrent queue owners. Lowering retention below the saved backlog
fails closed. Keep the SQLite database, rollback journal and lock together across
platform restarts; do not delete them during ordinary upgrades.

At capacity, a new record replaces the oldest in its namespace; a namespace with
no older record loses the new record. Each such drop is counted. Namespaces flush
in first-arrival order when the interval elapses or a namespace reaches 20 records.
Calls have at most 20 records and 40000 encoded bytes, splitting earlier if needed;
an individually oversized record is rejected before enqueue. Records are bounded
by their encoded size and count, with an additional 512 MiB SQLite page ceiling
(default 4 KiB pages), plus bounded rollback journal and lock files. SQLite reuses
freed pages. Reserve disk headroom for the journal. Disk/permission/corruption
errors stop the service; they are never acknowledged as successful enqueues.

Throttling, 5xx, transport and credential/permission failures retain batches for
retry after the configured interval (at least one second when interval is zero),
measured from completion of the failed request. The first retained failure stops
that namespace for the tick; queued full batches and new ingress cannot bypass its
cooldown. Other namespaces can still flush. Recognized HTTP-400 validation errors
reject the batch and increment `rejected`; unknown errors retain it, including
expired credentials. Retry cooldowns and the publish interval restart after restart. At most 100 calls per second
are initiated (10 ms minimum spacing). HTTPS and credential exchanges have
five-second connection/socket timeouts and a five-second total response deadline.
One serial ingress/flush loop bounds active work: a slow publisher gets one second
to finish a frame, listener backlog is 16, and flush I/O can delay ingestion and
shutdown. No availability or latency guarantee is made. A continuously failing
service requires operator monitoring; no metrics-based recursive error export occurs.

Delivery is **at least once**: a crash or ambiguous connection loss after AWS accepts
a request but before local removal can duplicate measurements. No exactly-once or
CloudWatch ingestion receipt is claimed. Request/response bodies, exceptions,
credentials and queue content are never logged. Namespace/name/dimension data
still reaches private disk and the configured AWS account; publishers must not put
secrets in measurements. There is no automatic redaction that silently changes
metric identity.

## Repeatable evidence

```sh
make test-cloudwatch-metrics SUIFLEET_DEV_IMAGE=suifleet-dev:ci
```

Only hash-locked public SDK preparation uses networking. Tests run as root in a
read-only, network-none disposable container with local HTTP fixture endpoints;
production CLI cannot enable plaintext upstream or substitute test clocks.
All 90 pinned fixture steps execute against the real queue and transport, with
emulator-observed Query API requests. Additional cases cover SF-019 telemetry numeric mapping/privacy, TLS key-log suppression, real kernel peer UID
grants, unauthorized callers, frame/payload bounds and splitting, private queue
access, singleton ownership, unavailable credentials, production CLI startup/clean
shutdown, outage/reopen recovery, exact wire values and independent botocore SigV4
comparison. SF-057 separately establishes real credential-provider isolation and
SDK resolution. No AWS account, physical Pi or production credential evidence is
claimed. No Move layouts or client migration is needed.
