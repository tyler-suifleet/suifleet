# Kinesis and S3 durable stream exporters (SF-058)

The opt-in Python companion [suifleet_stream_export.py](../sdk/python/suifleet_stream_export.py)
reads the real durable store through native IPC and exports through unmodified
boto3 1.43.114/botocore 1.43.114. It is a platform-managed component with a dedicated
UID, using [SF-057 scoped container credentials](AWS_CREDENTIALS.md). Sui identities
do not grant AWS access. No AWS resource or production credential is provisioned. The reference scope is
StreamManager **2.3.1**, rechecked on 2026-10-19 against the
[AWS component reference](https://docs.aws.amazon.com/greengrass/v2/developerguide/stream-manager-component.html).
This is native functionality, without its SDK wire protocol or shard-aware pacing.

## Operator configuration

Configure a dedicated stream and producer/exporter ACLs in the daemon:

```toml
[ipc.components.sensor]
uid = 2001
operations = ["stream_append"]
streams = ["measurements"]

[ipc.components.aws-export]
uid = 2002
operations = ["stream_describe", "stream_read", "stream_export_ack"]
streams = ["measurements"]

[streams]
max_streams = 4
max_total_bytes = 16777216

[[streams.definitions]]
name = "measurements"
max_size = 16777216
strategy_on_full = "RejectNewData"
persistence = "File"
external_export = true
discard_exported = true
```

Upgrade the daemon before enabling this additive IPC/configuration feature. Existing
non-external stream metadata omits the new false field, preserving its old format.
An older daemon cannot read external-export metadata/configuration; retain the new
daemon or follow an explicit drained/archived stream migration before downgrading.
No contract or chain migration is required.

External export is operator-only, requires file persistence, forbids TTL/overwrite
and cannot coexist with the built-in HTTP exporter. A full stream rejects appends
until confirmed export frees space; producers must handle `full`. The exporter
fails closed on corrupt segments. Crash-reserved sequence gaps are allowed;
sequence numbers never repeat. Only one destination/exporter is supported per
stream: do not grant `stream_export_ack` to a producer or another consumer.
That explicit operation plus the stream glob authorizes retiring confirmed data.
Ordinary `stream_read` permission cannot acknowledge it. Administrators remain trusted.

Install reviewed SDK files together in `/usr/libexec/suifleet/`, root-owned and
read-only. Install the hash-locked boto3 dependencies in a dedicated Python
environment using [requirements.txt](../sdk/credentials/requirements.txt);
Python 3.10+ is required. The provider, code, configuration and their parents must
not be writable by components. Provision the exporter's state directory as its
UID, mode 0700, under a protected parent. State contains stream payloads and AWS
multipart IDs, so protect backups too. No credentials are stored there.

Example protected `/etc/suifleet-aws/export.json`:

```json
{
  "service": "kinesis",
  "region": "us-east-1",
  "stream": "measurements",
  "component": "aws-export",
  "source_id": "provisioned-unique-device-id",
  "socket": "/run/suifleet/ipc.sock",
  "state_dir": "/var/lib/suifleet-aws-export",
  "destination": "provisioned-kinesis-stream",
  "batch_messages": 100,
  "batch_bytes": 1048576,
  "bytes_per_second": 1048576,
  "poll_seconds": 5,
  "max_backoff_seconds": 300
}
```

For S3, set `service` to `s3`, `destination` to
`provisioned-bucket/exclusive-device-prefix/` and optionally `part_bytes` to
5242880 (default, minimum 5 MiB; maximum 16 MiB). Provision an exclusive prefix
for this stream/exporter and an AbortIncompleteMultipartUpload lifecycle rule
as defense against abandoned uploads after loss of the local state. The program
never creates a bucket, stream, policy or lifecycle rule. Normal AWS endpoints use
TLS and standard certificate validation. The optional `endpoint` override accepts
only loopback HTTP for local API emulators; it cannot redirect credentials to a
remote endpoint. SDK proxies and automatic SDK retries are disabled.

Launch with the SF-057 wrapper, as the mapped exporter UID:

```sh
python3 /usr/libexec/suifleet/suifleet_aws.py \
  --socket /run/suifleet-aws/credentials.sock --component aws-export -- \
  /opt/suifleet-aws/bin/python /usr/libexec/suifleet/suifleet_stream_export.py \
  --config /etc/suifleet-aws/export.json
```

Use a separate SF-057 provider/grant with the intended IAM role scope. The exporter
requires SDK credentials with method `container-role`; static environment/profile
credentials are refused. The wrapper removes inherited alternative credential
sources. Do not provision alternative profiles in this UID's home.

A platform-owned systemd service can use `User=aws-export`, `UMask=0077`,
`StateDirectory=suifleet-aws-export`, `StateDirectoryMode=0700`,
`NoNewPrivileges=yes`, `CapabilityBoundingSet=`, `ProtectSystem=strict`,
`ProtectHome=yes`, `PrivateTmp=yes`, `LimitCORE=0`, `Restart=always`,
`RestartSec=5` and `RuntimeMaxSec=3000` for a provider token TTL of 3600 seconds.
The absolute ExecStart is the wrapper invocation above. This obtains a fresh
per-start token before its expiry; AWS credentials refresh through the normal
SDK provider. Keep IPC/provider sockets reachable. Do not use the daemon's
shared-UID supervisor for credential-bearing components. Explicit component
lifecycle commands may start/stop that pre-provisioned unit through a narrowly
authorized platform helper; recipe translation and automatic service installation
are not supplied here. The supported integration is native IPC/Python SDK plus
the existing scoped-credential wrapper, not the AWS Stream Manager SDK wire API.

## Delivery and recovery

Every batch is saved with fsync + atomic rename + directory fsync before a cloud
write. A file lock fences concurrent exporters using the same state directory.
The checkpoint binds all configuration, including destination and limits. Do not
delete/reuse state, change the destination, or recreate a source stream while work
is pending. For reconfiguration, stop producers, drain the pending batch, stop the
exporter and archive its state; restarting with a fresh directory resumes the
daemon's confirmed checkpoint. A destination change should use a new source
stream to preserve the old destination's history.

The local delivered marker is persisted before the monotonic daemon acknowledgement.
The daemon persists that acknowledgement before discarding data. If the IPC answer
or local checkpoint write is lost, restart repeats the local acknowledgement and
does not resend a batch with a persisted delivered marker. Disk/fsync failures
retain pending work; the next attempt re-persists it before cloud writes. Atomicity
assumes the host filesystem honors fsync; physical power-loss remains untested.

**Kinesis:** `PutRecords` sends envelopes containing schema, configured unique source_id, local stream,
sequence, appended time, sequence_gap_before and payload (or data_base64 for non-JSON data).
The stable partition key is SHA-256(canonical JSON [source_id, stream name]). Only entries with both
SequenceNumber and ShardId are confirmed; response count/FailedRecordCount are
validated. Successful entries are persisted individually, failed entries retry.
The daemon checkpoint advances only after the entire pending batch succeeds.
A response lost before local persistence can produce duplicates: consumers must
deduplicate by source_id, stream and sequence. Provision a unique stable source_id
per device; never recycle it for a recreated stream. PutRecords and partial
retries do not guarantee ordering; sequence numbers permit consumer reordering. sequence_gap_before explicitly reports
numbering gaps (including crash reservations and any pre-existing retention gap)
instead of silently skipping them.
Conservative limits are 500 records, 1 MiB per record including partition key and
5 MiB per request, even where newer AWS quotas permit more.

**S3:** Each snapshot becomes JSON Lines with the same envelopes. Object keys
include the stream hash, first/last sequence and complete body SHA-256, under the
exclusive prefix. All uploads use multipart, including a single final small part.
The upload ID and each UploadPart ETag are saved. Restart resumes the first
unacknowledged part; an ambiguous part write safely replaces the same part number.
CompleteMultipartUpload uses saved part numbers/ETags; botocore parses embedded
errors even with HTTP 200. HeadObject checks size and sha256 metadata before local
acknowledgement. A lost completion response is reconciled by that deterministic
object identity. This relies on trusted exclusive writers, not hostile metadata;
it is not an independent download/hash verification of AWS storage.

A lost initiation response is recovered by listing/aborting uploads for the
**exact deterministic key** before starting another one. Listing is paginated;
other keys are untouched. A missing/aborted upload resets multipart progress,
retaining the snapshot. Provision ListBucketMultipartUploads permission and the
exclusive prefix; otherwise cleanup blocks rather than silently leaking state.

To abort incomplete remote work explicitly while retaining the snapshot and
checkpoint, run the same wrapper/command with `--abort`, after stopping the
normal service. The next normal start retries. It never deletes completed objects
or acknowledges unexported data. Preserve the private local state across restarts.

**Limits:** `batch_messages` is 1–500; `batch_bytes` bounds the outgoing decoded
envelopes (1 KiB–16 MiB, Kinesis at most 5 MiB). A single record exceeding the
configured batch budget blocks export instead of bypassing it. The exporter reads
one IPC message at a time within the existing 64 KiB response-frame limit; large
native messages that cannot fit that response block rather than disappear.
One durable snapshot adds at most about 4/3 of batch_bytes plus JSON overhead
beside the source storage. Multipart bodies/SDK buffers add bounded memory.
`bytes_per_second` conservatively paces each data request, including retries,
before transmission; HTTP/signature/metadata overhead is excluded and pacing
restarts conservatively after a process restart. Requests are serial. Polling is
1–3600 seconds; failures (including 403) back off 1, 2, 4… to the configured cap
(1–3600 seconds), indefinitely without dropping data. Generic logs omit exception
details, payloads and credentials. Inspect the protected checkpoint and daemon
`stream_describe` for progress; configure platform alerts for persistent failures.

## AWS permissions and remaining acceptance

Provision least-privilege IAM separately:
- Kinesis: `kinesis:PutRecords` for the selected stream ARN.
- S3: `s3:PutObject`, `s3:GetObject` (HeadObject),
  `s3:AbortMultipartUpload` for the exclusive object prefix, and
  `s3:ListBucketMultipartUploads` for the bucket, narrowed using supported IAM
  conditions. Encrypted buckets/streams may need explicitly provisioned KMS grants.

Reference semantics:
[AWS PutRecords](https://docs.aws.amazon.com/kinesis/latest/APIReference/API_PutRecords.html),
[S3 multipart upload](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html),
[CompleteMultipartUpload embedded errors](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html).
Verify IAM/KMS/region, credentials, TLS and bucket policies with authorized live
accounts before deployment.

Run `make test-stream-export SUIFLEET_DEV_IMAGE=suifleet-dev:ci`. Public hash-locked
dependency preparation/build runs first; the actual exporter and daemon tests run
in a disposable network-none container. Tests use unmodified boto3 signed HTTP
requests, a local JSON/XML AWS API emulator and a local SDK credential endpoint.
Moto supplies independent boto3 Kinesis/GetRecords and S3/GetObject read-back;
the export-checkpoint sequence outcomes from stream-cases.json are exercised
through the real exporter, and the full raw-byte fixture runs in Rust store tests.
They assert payloads and exercise partial success, denied requests, lost initiation,
part/completion/Kinesis/local-ack replies, embedded errors, multipart daemon crash
resume, batching/pacing, config fencing, wrong credential sources and distinct-UID
ack denial. Moto test dependencies are pinned/hashes in
[sdk/stream-export/requirements.txt](../sdk/stream-export/requirements.txt);
preparation selects the pinned container CPython 3.10 ABI explicitly. Existing SF-057 tests separately exercise the real credential adapter
and mTLS upstream. Emulators do not establish AWS-account behavior.

**Live AWS acceptance is unrun and unauthorized, retained in SF-058/#103.**
Physical Pi integration remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
SiteWise/other service exporters remain [SF-064/#109](https://github.com/tyler-suifleet/suifleet/issues/109).
No complete Stream Manager, AWS account, production, hardware-token or firmware
safety acceptance is claimed.
