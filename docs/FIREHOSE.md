# Firehose durable stream export (SF-077)

[`scripts/firehose_export.py`](../scripts/firehose_export.py) is an opt-in,
platform-managed Python 3.10+ Linux companion. It reads configured SF-034 durable
streams through native IPC and uses scoped SF-057 credentials for Firehose
`PutRecordBatch`. It is a clean-room functional alternative, not execution of
AWS's Lambda component, AWS recipe compatibility, or Kinesis Data Streams.
No AWS component source, binaries or license-restricted implementation was used.

The versioned contract and upstream references reviewed before implementation are
in [`firehose-v1.json`](../interfaces/compat/firehose-v1.json):
[Greengrass Firehose 2.1.10](https://docs.aws.amazon.com/greengrass/v2/developerguide/kinesis-firehose-component.html)
and [Firehose API 2015-08-04 PutRecordBatch](https://docs.aws.amazon.com/firehose/latest/APIReference/API_PutRecordBatch.html).
The pinned `botocore` 1.43.114 service model supplies SigV4, serialization and
refreshable container credentials; install the reviewed hashed dependency set:

```sh
python3 -m pip install --require-hashes -r sdk/credentials/requirements.txt
```

## Operator configuration

Install code and dependencies outside writable component workspaces. Install
`sdk/python/suifleet_ipc.py` beside the exporter in the installed code directory. Provision a
separate platform-managed service UID as described in [AWS_CREDENTIALS.md](AWS_CREDENTIALS.md).
Grant that exact component/UID `stream_describe` and `stream_read` for the source
stream in the native IPC ACL, and a credential grant on a dedicated SF-057
provider scoped by IAM to `firehose:PutRecordBatch` on the delivery stream ARN.
The exporter explicitly loads only the container provider; ambient static/profile
credentials are not a fallback. The provider refreshes AWS credentials; restart
the wrapper before its per-process bearer token expires. No credential provisioning
or AWS resource creation is automatic.

Use one immutable source/destination mapping per private checkpoint directory:

```json
{
  "version": 1,
  "socket": "/run/suifleet/ipc.sock",
  "component": "firehose-exporter",
  "stream": "telemetry",
  "region": "us-east-1",
  "delivery_stream": "telemetry-delivery",
  "state_dir": "/var/lib/suifleet-firehose/telemetry"
}
```

Run under the credential wrapper, in the foreground, as the dedicated UID:

```sh
python3 /usr/libexec/suifleet/suifleet_aws.py \
  --socket /run/suifleet-aws/credentials.sock --component firehose-exporter -- \
  python3 /usr/libexec/suifleet/firehose_export.py --config /etc/suifleet/firehose.json
```

The configuration and its ancestors must be operator-controlled and read-only to
producers. The state directory must be exporter-owned, mode 0700, on a local
filesystem supporting flock, atomic rename and fsync. Ancestors must be trusted,
with no component-writable paths or symlinks. Do not share its UID with untrusted
components. Root and the exporter UID are trusted. Configure the platform unit with
`UMask=0077`, `NoNewPrivileges=yes`, core dumps disabled and appropriate filesystem
and network restrictions. Provision the IPC socket and AWS service access; do not
mount AWS profiles or expose SDK debug logs. One process holds an exclusive flock;
a second exporter using the same state is rejected. Different state directories
are independent consumers and can duplicate all data.

Use a `File` stream without a native HTTP export. Start with `RejectNewData`, no TTL,
and sufficient quota for the expected outage; the exporter never deletes source
records or advances the native HTTP export checkpoint. Retention is operator-owned:
plan explicit archival/rotation when the bounded stream fills. TTL/overwrite may
remove unexported records; an evicted read or a sequence gap stops progress and
retries with a redacted diagnostic. Inspect retained data and reconcile the source
and destination explicitly rather than silently skipping. Sequence reservation
gaps after a daemon crash also require reconciliation. Do not delete/recreate a
source under an existing mapping: sequences identify one stream lifetime. For a
new source lifetime or changed mapping, reconcile pending data and provision a new
private state directory. Configuration changes cannot reuse an existing checkpoint.

## Version 1 delivery semantics

Each IPC JSON payload becomes compact UTF-8 JSON plus LF, with no sequence wrapper.
This is a JSON stream exporter, not arbitrary binary payload support. The SDK
base64 encodes each record once and signs for service `firehose`. Requests carry
at most 500 records, 1,024,000 bytes per record and 4,194,304 raw bytes per batch,
including the LF. Source reads request one record at a time to respect IPC framing.
Memory is bounded by one batch plus SDK encoding overhead, independent of backlog.
An oversized record fails closed; no data is truncated or discarded.

Results are matched by array index. Only an explicit nonempty `RecordId` without
an error acknowledges a record. A malformed/missing result or inconsistent
`FailedPutCount` retains the entire batch. For a valid partial result, the durable
checkpoint advances only across the acknowledged prefix. All later records are
retried, including later successes: this deliberately preserves the SF-064 v1
prefix contract rather than claiming selective retry/deduplication. Firehose does
not provide sequence deduplication or ordering guarantees at the destination.

The checkpoint is destination/source/configuration-bound, atomically replaced,
file-synced and directory-synced before in-memory advancement. Corruption or a
binding mismatch rejects startup. A crash or lost response may duplicate accepted
records. Firehose acceptance does not establish downstream delivery or permanence;
service-side retention and destination behavior remain AWS responsibilities.

A timeout, outage, IAM denial, expired credential or permanent refusal retains
records. SDK automatic operation retries are disabled; the foreground loop backs
off 1, 2, 4, ... 60 seconds, capped at 60, indefinitely. Idle polling is 1 second.
There is no busy-loop on partial failure, no cloud resource creation, no automatic
reboot, and no silent dead-letter drop. Diagnostics omit SDK text/payloads/tokens.
Monitor the platform service and checkpoint; operator intervention is necessary for
persistent configuration, quota, retention, permission or credential failures.

## Evidence and limits

Prepare dependencies, build the debug daemon and gRPC simulator shim, then run:

```sh
python3 -m pip install --require-hashes --target .ci-cache/aws-python -r sdk/credentials/requirements.txt
# In the pinned development environment:
cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
# From the host, using the pinned built image:
make test-firehose SUIFLEET_DEV_IMAGE=suifleet-dev:ci
```

The network-none suite executes signed SDK HTTP calls to local Firehose/container
credential emulators: count/byte bounds, indexed partial failure, retained suffix,
checkpoint reopen, service outage, expired token, failed mandatory credential
refresh and recovery, ambiguous disconnect, malformed results, failed fsync,
exclusive ownership, binding/corruption, unsupported versions and sequence gaps.
It also runs the real daemon stream suite with a Firehose path: actual durable
appends, peer-UID IPC reads, SDK wire requests and exporter checkpoint reopen.
SF-064's pure reference vectors remain specification-only; their catalog fields
remain pending and are not runtime evidence. Ordinary CI runs this separate suite.

Live AWS acceptance is **unrun and unauthorized**, tracked separately in
[SF-077/#131](https://github.com/tyler-suifleet/suifleet/issues/131): verify IAM scope,
real Firehose ingestion/destination, refresh, outage, retry/cost and downstream
duplicate handling only after explicit AWS authorization. Physical integration
is deferred to [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
No AWS account mutations, paid services, production credentials, physical image
or full-parity result is claimed. Sui contracts/transactions are unchanged;
automatic testnet CI remains the normal PR gate.
