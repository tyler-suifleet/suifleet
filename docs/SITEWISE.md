# SiteWise processing and publishing (SF-071)

The opt-in Linux Python 3.10+ companion [sitewise_export.py](../scripts/sitewise_export.py)
reads SF-070 industrial schema 1 records through authenticated SF-034 IPC and
publishes SiteWise TQVs with SF-057 scoped container credentials. This is original
clean-room code based on public API documentation, not AWS component execution,
AWS recipe compatibility, asset-model formula evaluation or full SiteWise Edge parity.
The [versioned contract](../interfaces/compat/sitewise-v1.json) pins the reviewed
upstream API 2019-12-02 and botocore 1.43.114 model. No proprietary component source
or binaries were used. Independent specification/source review is a PR merge gate.

## Provisioning

Install `scripts/sitewise_export.py` with `sdk/python/suifleet_ipc.py` available in
its code directory (or the repository-relative SDK layout), root-owned/read-only,
outside writable component workspaces. Install the exact hashed dependencies:

```sh
python3 -m pip install --require-hashes -r sdk/credentials/requirements.txt
```

Run as a dedicated platform-managed UID, separate from producers and the daemon.
Use [SF-057](AWS_CREDENTIALS.md) with a dedicated provider/IAM role granting only
`iotsitewise:BatchPutAssetPropertyValue` on the intended properties/aliases.
Alias authorization must use the appropriate SiteWise IAM conditions; a local IPC
grant is not an IAM policy. The SDK loads only the wrapper's container credential
provider, never ambient static/profile credentials. It refreshes credentials;
restart the wrapper before its per-process bearer token expires. No account,
asset, alias, role, certificate or production credential is provisioned here.

```toml
[ipc.components.sitewise]
uid = 2002
operations = ["stream_describe", "stream_read", "stream_export_ack"]
streams = ["industrial"]

[[streams.definitions]]
name = "industrial"
max_size = 67108864
strategy_on_full = "RejectNewData"
persistence = "File"
external_export = true
discard_exported = true
```

Give the industrial producer only append permission on this stream. No other
consumer may acknowledge it. External-export streams forbid TTL, overwrite and
native HTTP export. Full streams backpressure the collector; never shrink their
quota below stored data. This publisher durably checkpoints service acceptance
before acknowledging local retirement. Do not reset/delete/recreate the source
or checkpoint independently. A daemon checkpoint ahead of local state refuses
startup progress; restore consistent backups or reconcile explicitly.

Example immutable configuration:

```json
{
  "version": 1,
  "socket": "/run/suifleet/ipc.sock",
  "component": "sitewise",
  "stream": "industrial",
  "region": "us-east-1",
  "state_dir": "/var/lib/suifleet-sitewise/industrial",
  "mappings": [
    {"source": "plc1", "point": "temperature", "valueType": "D", "propertyAlias": "/factory/temperature", "scale": 0.1, "offset": 0}
  ]
}
```

Alternatively use both `assetId` and `propertyId` (nonzero lowercase UUIDs) instead
of `propertyAlias`. Each source/point pair has exactly one mapping, at most 256.
Aliases must be nonempty, at most 1000 characters, with no ASCII control characters.
Mappings do not create or update SiteWise properties. Map every sample in the
source stream; an unmapped sample retains the entire message.

```sh
python3 /usr/libexec/suifleet/suifleet_aws.py \
  --socket /run/suifleet-aws/credentials.sock --component sitewise -- \
  python3 /usr/libexec/suifleet/sitewise_export.py --config /etc/suifleet/sitewise.json
```

Configuration/code and all ancestors must be operator-controlled and read-only
to producers. State must be exporter-owned mode 0700 on a local filesystem
supporting flock, atomic rename and fsync; ancestors must be trusted and contain
no symlinks or component-writable directories. Do not share the exporter UID.
Use `UMask=0077`, disable core dumps, restrict network/filesystem access and avoid
SDK debug/environment dumps. Root and the exporter UID are trusted. State binds
the entire configuration by SHA-256 and contains no credentials. Changed mappings
require a new source/state lifetime after explicit reconciliation. One exclusive
flock prevents concurrent use of a checkpoint; independent state directories
would create independent consumers and must never share this source's ack grant.

## Version 1 semantics

Each source message is fully validated before sending. It contains 1–1024
industrial records with integer schema 1. Samples use `(source, point)` mapping.
Collector `status` records are control-plane events with no TQV and are explicitly
excluded from publishing; a status-only message is checkpointed without a request.
A `gap` or unknown record blocks the message for operator reconciliation; gaps
are not measurements and are never disguised as successful SiteWise ingestion.

Every mapping requires `valueType`: `D` (SiteWise DOUBLE), `I` (INTEGER),
`B` (BOOLEAN) or `S` (STRING), matching the destination property. Numeric collector
types support D/I and apply `value * scale + offset`. D results must be within
±10^10, the pinned API model's doubleValue range. I results must be integral and
within signed int32; fractional values are refused, never truncated. Integer
source values outside ±2^53 are refused before affine conversion to avoid silent
rounding. Non-finite inputs/results are refused. Numeric transformations use
Python IEEE-754 arithmetic; this is configured affine processing, not arbitrary
expressions, aggregates, asset-model evaluation or windowed metrics.

B accepts Modbus `bool` and OPC UA `boolean`. S accepts collector `string` and
OPC UA `datetime` (the collector's integer Unix nanoseconds become exact decimal
text, without date arithmetic or floating-point conversion).
Strings may be empty, have at most 1024 characters, and must contain no U+0000–001F
or U+007F controls. No implicit string/numeric/boolean conversion is performed;
non-null source/destination mismatches are refused. Scale/offset are only valid
for D/I mappings.

Non-null GOOD/BAD/UNCERTAIN quality is preserved. Any null sample uses SiteWise
`nullValue` with the mapping's declared D/I/B/S type, including untyped OPC UA
points during disconnect or missing-value events. GOOD nulls (including OPC UA
null String and unset DateTime scalars) are downgraded to BAD: absent values
never claim good measurement quality and do not stall delivery. Existing BAD or
UNCERTAIN quality and source timestamps are preserved. An
untyped point's subsequent non-null values must have a supported collector type
matching the mapping; unsupported non-null types block the message. Original integer `timestamp_ns`
is split into seconds/nanoseconds without floating-point conversion. API timestamp
bounds are checked; timestamps are never replaced with ingestion time. SiteWise's
[-7 days, +10 minutes] window can permanently reject an outage backlog; those
records remain pending, requiring explicit operator reconciliation.

Requests have at most 10 entries, one TQV per entry, with deterministic entry IDs
within the source message. `enablePartialEntryProcessing=false` prevents partial
acceptance inside an entry. Any valid service error retains the whole source
message; successful earlier requests/entries may repeat on retry. Unknown,
duplicate or malformed error IDs/results retain the message. Only an explicit
`errorEntries: []` for every request permits checkpoint advancement. A returned
HTTP success alone is insufficient. AWS overwrites duplicate timestamp/quality
TQVs; there is no general exactly-once or downstream durability claim.

Checkpoint files are atomically replaced, file-synced and directory-synced before
local stream acknowledgement. Lost acks are retried before reading the next
message without cloud resend. A crash/lost response before checkpoint persistence
may repeat accepted writes. Corrupt state, source corruption, unsafe source mode
and inconsistent checkpoints fail closed. Crash-reserved sequence gaps are
supported in lossless external streams. Memory is bounded by one IPC message
(the daemon framing bound), at most 1024 entries and one 10-entry SDK request.
No in-memory backlog grows with outages.

The foreground loop retries errors/partial results at 1, 2, 4, ... 60 seconds,
capped at 60; idle polling is one second. SDK operation retries are disabled.
Permanent errors are retained indefinitely, never dropped/dead-lettered implicitly.
Diagnostics expose only fixed codes, never SDK error text, payloads or credentials.
Monitor the platform service and both checkpoints. No reboot or platform updater
behavior is added.

## Verification and remaining acceptance

After building the debug daemon and gRPC simulator example, run:

```sh
python3 -m pip install --require-hashes --target .ci-cache/aws-python -r sdk/credentials/requirements.txt
make test-sitewise SUIFLEET_DEV_IMAGE=suifleet-dev:ci
```

The network-none suite checks actual SDK JSON requests and independently verifies
SigV4 signatures against local service/container credential emulators. It covers
transformation/types/quality/nanoseconds, property/alias mapping, chunk bounds,
partial failures, rejected/expired data, malformed responses, IAM/credential
failures, disconnects, restart/checkpoint binding/corruption/fsync/locking, lost
local ack and real daemon IPC/retirement plus SIGKILL reservation-gap recovery.
Ordinary CI runs this suite; normal automatic Sui testnet CI remains required.
There are no contract changes, schema migrations or chain operations in this feature.

Live AWS acceptance is **unrun and unauthorized**, tracked separately within
[SF-071/#125](https://github.com/tyler-suifleet/suifleet/issues/125): real scoped IAM,
property/alias ingestion, time-window refusals, refresh/outage/recovery and costs
require explicit authorization. The local emulator proves the tested wire contract,
not AWS service acceptance. Physical Pi integration remains
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32), deferred by the
user. No AWS account mutations, paid service, production credentials, physical
programming or full-parity result is claimed. SF-064 reference vectors remain
specification-only; runtime evidence is this separate publisher suite.
