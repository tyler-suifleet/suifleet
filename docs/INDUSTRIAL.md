# Industrial Modbus and OPC UA collection (SF-070)

`suifleet-industrial` 1.0.0 is a native component that polls Modbus devices and
polls or subscribes to OPC UA servers. It appends typed, timestamped,
quality-tagged records to a [durable stream](STREAMS.md) through the
authenticated [component IPC socket](COMPONENT_IPC.md). The daemon exports that
stream in batches signed by the device key, so collector output is authenticated
from the IPC peer identity to the receiver. An optional local pub/sub topic gets
a copy of each record. Deterministic Modbus and OPC UA simulators come with it
for tests and demonstrations.

This is a clean-room SuiFleet implementation of the published protocols. It
does not run, wrap or reproduce the AWS `aws.greengrass.Modbus` Lambda or the
SiteWise Edge OPC UA collector, and it makes no AWS recipe, API or SiteWise
compatibility claim (see [differences](#differences-from-the-aws-components)).

## Components and code

| Path | Contents |
|---|---|
| [`scripts/industrial/collector.py`](../scripts/industrial/collector.py) | Configuration v1, sources, bounded buffer, stream writer, CLI |
| [`scripts/industrial/modbus.py`](../scripts/industrial/modbus.py) | Modbus PDU/MBAP/RTU codec, TCP/TLS/RTU clients, simulators |
| [`scripts/industrial/opcua_codec.py`](../scripts/industrial/opcua_codec.py) | OPC UA Binary encoding subset and service structures |
| [`scripts/industrial/opcua_secure.py`](../scripts/industrial/opcua_secure.py) | UA TCP and Secure Conversation: None, Basic256Sha256 |
| [`scripts/industrial/opcua_client.py`](../scripts/industrial/opcua_client.py) | Discovery, sessions, Read, subscriptions, token renewal |
| [`scripts/industrial/opcua_server.py`](../scripts/industrial/opcua_server.py) | Deterministic OPC UA simulator with fault controls |
| [`scripts/industrial/build.py`](../scripts/industrial/build.py) | Deterministic zip application build |
| [`interfaces/compat/industrial/`](../interfaces/compat/industrial) | Configuration vectors and the pinned optional `cryptography` lock |

The collector needs Python 3.10 or later, standard library only. OPC UA
policy `Basic256Sha256` also needs the `cryptography` package. If it is missing,
configuration loading refuses such a source; it never falls back to policy None.
[`crypto-lock.json`](../interfaces/compat/industrial/crypto-lock.json) pins
`cryptography` 44.0.3, `cffi` 1.17.1 and `pycparser` 2.22 wheels for CPython
3.10 on x86_64 and aarch64 (glibc ≥ 2.28) by SHA-256. On a device, an
equivalent distribution package (for example Yocto `python3-cryptography`)
serves the same purpose. SuiFleet does not redistribute these packages.

## Build and deploy

```bash
cd scripts
python3 -m industrial.build --out /tmp/suifleet-industrial-1.0.0.pyz   # prints the SHA-256
python3 -I /tmp/suifleet-industrial-1.0.0.pyz --config collector.json --check-config
python3 -I /tmp/suifleet-industrial-1.0.0.pyz --config collector.json --status-file /run/industrial/status.json
```

The build writes a fixed member order with fixed timestamps and permissions, so
the same sources always produce the same bytes. Deliver the archive as an
authenticated artifact and pin it with the recipe digest. The archive contains
the collector package and the IPC SDK and nothing else.

Components are authenticated by the IPC peer UID, so the collector runs as its
own platform-managed Unix user and service. It is not one of the daemon's
shared-UID supervised processes, which are denied IPC. Grant it only what it
uses:

```toml
[ipc.components.industrial]
uid = 2101
operations = ["stream_append", "stream_describe", "publish"]   # publish only with output.topic
streams = ["industrial"]
publish = ["industrial/samples"]

[[streams.definitions]]
name = "industrial"
max_size = 67108864
strategy_on_full = "RejectNewData"     # full -> collector backpressure, not silent eviction
persistence = "File"
```

Configuration, keys, certificates and password files must be regular files
owned by the collector user, without group or other access (`chmod 600`).
Passwords are never inline in the configuration, never logged and never
written to records. `SIGTERM` stops polling and flushes the buffer for up to
five seconds. The collector exits 0 only when every buffered record was appended.

## Configuration, version 1

The configuration is one JSON object. The integer `version: 1` is checked
before anything else. A missing, boolean, string or other version is refused
with `unsupported-version`, and no connection is attempted. Unknown fields are
errors, as are wrong types (a boolean is not an integer) and out-of-range values.
[`config-cases.json`](../interfaces/compat/industrial/config-cases.json) holds 53
valid and invalid vectors.

```json
{
  "version": 1,
  "collector_id": "line1-collector",
  "output": {"socket": "/run/suifleet/ipc.sock", "component": "industrial", "stream": "industrial"},
  "sources": [
    {"name": "plc1", "protocol": "modbus", "transport": "tcp", "host": "192.0.2.10", "unit_id": 1,
     "poll_interval_ms": 1000,
     "points": [{"name": "temperature", "table": "holding_register", "address": 0, "type": "float32"},
                {"name": "running", "table": "coil", "address": 0, "type": "bool"}]},
    {"name": "opc1", "protocol": "opcua", "endpoint_url": "opc.tcp://192.0.2.20:4840",
     "security_policy": "Basic256Sha256", "security_mode": "SignAndEncrypt",
     "certificate_file": "/etc/suifleet/industrial/client.der",
     "private_key_file": "/etc/suifleet/industrial/client.key",
     "server_certificate_file": "/etc/suifleet/industrial/server.der",
     "identity": {"type": "username", "username": "collector", "password_file": "/etc/suifleet/industrial/opc1.password"},
     "points": [{"name": "flow", "node_id": "ns=2;s=Line1.Flow", "type": "double"},
                {"name": "mode", "node_id": "ns=2;i=1001", "mode": "poll", "poll_interval_ms": 5000}]}
  ]
}
```

| Field | Default | Bounds |
|---|---|---|
| `collector_id`, source and point `name` | — | `[A-Za-z0-9._-]`, 1-64; unique |
| `output.socket` | — | absolute path |
| `output.stream` | — | SF-034 stream name |
| `output.topic` | none | exact topic, no wildcards; best-effort copy per record |
| `output.max_buffered_records` | 10000 | 1-100000 |
| `output.max_records_per_message` | 100 | 1-1000; a message also stays under 60000 bytes |
| `output.retry_min_ms` / `retry_max_ms` | 200 / 30000 | 10-60000 / min-300000 |
| `output.ipc_timeout_ms` | 10000 | 100-120000 |
| `sources` | — | 1-64 sources, 1-1000 points each, at most 10000 points in total |
| `connect_timeout_ms` / `request_timeout_ms` | 5000 / 3000 | 100-60000 |
| `reconnect_min_ms` / `reconnect_max_ms` | 1000 / 60000 | 100-600000 / min-3600000 |

**Modbus sources.**

| Field | Values |
|---|---|
| `transport` | `tcp`, `tls` or `rtu` |
| `host`, `port` | `tcp` and `tls` only; the port defaults to 502 (`tcp`) or 802 (`tls`) |
| `unit_id` | 0-255 over TCP; 1-247 over RTU |
| `tls` | `ca_file`, `certificate_file`, `private_key_file` and an optional `server_name` |
| `serial` | `device`, plus `baud_rate` (1200-115200, default 9600), `data_bits` (7 or 8), `parity` (`N`, `E` or `O`) and `stop_bits` (1 or 2) |
| `poll_interval_ms` | 50-86400000 |
| `max_registers_per_request` | 1-125 |
| `max_bits_per_request` | 1-2000 |

Each point names a `table` and an `address` (0-65535). The table is one of
`coil`, `discrete_input`, `holding_register` or `input_register`.

- Coils and discrete inputs have `type: bool`.
- Registers have one of the types `int16`, `uint16`, `int32`, `uint32`,
  `float32`, `int64`, `uint64` or `float64`.
- Multi-register values take `word_order`, `big` (the default) or `little`.
  Bytes within a register are always big-endian.

**OPC UA sources.**

| Field | Values |
|---|---|
| `endpoint_url` | `opc.tcp://` |
| `security_policy`, `security_mode` | `None` with `None`, or `Basic256Sha256` with `Sign` or `SignAndEncrypt`. A secure policy requires `certificate_file`, `private_key_file` and `server_certificate_file` (DER or PEM; RSA 2048-4096) |
| `identity` | `{"type": "anonymous"}` or `{"type": "username", "username": …, "password_file": …}` |
| `allow_plaintext_password` | default `false` |
| `publishing_interval_ms` | 50-3600000 |
| `keepalive_count` | 1-1000 |
| `lifetime_count` | at least 3 × `keepalive_count`, up to 30000 |
| `session_timeout_ms` | 10000-3600000 |
| `max_notifications_per_publish` | 1-10000; at most `output.max_buffered_records` |

Each point names a `node_id` in the standard string form (`ns=2;s=Line1.Flow`,
`i=2258`, `g=…` or `b=…`).

- `mode` is `subscribe` (the default) or `poll`.
- Subscribed points take `sampling_interval_ms` (0-3600000; the default is the
  publishing interval) and `queue_size` (1-100, default 10).
- Polled points take `poll_interval_ms` (50-86400000, default 1000). A source may
  have at most `output.max_buffered_records` polled points, so one Read always
  fits the buffer.
- An optional expected `type` is one of `boolean`, `sbyte`, `byte`, `int16`,
  `uint16`, `int32`, `uint32`, `int64`, `uint64`, `float`, `double`, `string` or
  `datetime`.

## Records

Each stream message is
`{"schema": 1, "collector": ID, "batch": "<boot>:<n>", "records": [...]}`.
The boot identifier is random per process start. A retried append, after an IPC
timeout whose outcome is unknown, may store a message twice. Consumers
deduplicate by `batch` (a retry resends the identical message), or by the
record `id`, which is `<collector>:<boot>:<counter>`. Counters are contiguous
within a boot, except for records dropped when the buffer was full; a `gap`
record counts those in `dropped_records`.

| `kind` | Fields |
|---|---|
| `sample` | `source`, `protocol`, `point`, `type`, `value`, `quality`, `status`, `timestamp_ns`, `source_timestamp_ns`, `server_timestamp_ns`, `received_ns` |
| `status` | `source`, `protocol`, `state` (`connected`, `subscribed`, `resubscribing`, `disconnected`, `rejected`), `reason`, `received_ns` |
| `gap` | `source`, `protocol`, `reason` (`backpressure` or `overrun`), `skipped_cycles`, `dropped_records`, `from_ns`, `to_ns`, `received_ns` |

**Quality** is `GOOD`, `UNCERTAIN` or `BAD`.

- OPC UA takes it from the StatusCode severity bits, and `status` carries the
  name, such as `UncertainLastUsableValue` or `BadNodeIdUnknown`. `+Overflow`
  means the server's monitored-item queue discarded values.
- Modbus values are `GOOD` (`status: good`). An exception response is `BAD`
  with a status such as `exception:2 illegal-data-address`.
- Values the collector cannot represent are `BAD` with a `null` value:
  `non-finite` (NaN or infinity), `BadTypeMismatch(received …)` (a configured
  type differs from the actual Variant type), `unsupported-type` (arrays,
  ByteString or structures) and `too-large` (strings over 1 KiB).
- A lost or refused connection writes one `BAD` sample per point, with status
  `disconnected` or `rejected`. The detailed reason goes in the `status`
  record. A failure that persists is not repeated every cycle.

**Timestamps** are integer nanoseconds since the Unix epoch.

- `timestamp_ns` is the OPC UA SourceTimestamp, or for Modbus, which has no
  device time, the moment the response arrived.
- `source_timestamp_ns` and `server_timestamp_ns` are the OPC UA DataValue
  times, or `null`.
- `received_ns` is the collector's clock. DateTime values become nanoseconds;
  OPC UA `0` and `Int64.Max` mean no time and become `null`.

## Protocol behavior

**Modbus** (V1.1b3; TCP Implementation Guide V1.0b; Serial Line V1.02).

- **Requests.** Function codes 01-04 only; the collector never writes to a
  device. Adjacent points in the same table share one request, within the
  per-request limits.
- **Response checks.** Every response is checked: transaction identifier,
  protocol identifier, MBAP length, unit, function code, byte count, unused
  coil bits, and the CRC over RTU.
- **Errors.** A malformed or unanswered response closes the connection, because
  the byte stream may be out of step. An exception response affects only the
  points in its request and keeps the connection open.
- **TLS** is TLS 1.2 or later with mutual certificate authentication: the
  configured CA, the client certificate and host-name verification. It never
  falls back to plaintext.
- **RTU** uses termios raw mode, 3.5 character times of inter-frame silence and
  CRC-16/MODBUS.

**OPC UA** (OPC 10000-4 and 10000-6 release 1.05).

- **Discovery.** GetEndpoints runs on a policy None discovery channel. The
  collector continues only if the server offers exactly the configured policy
  and mode over the UA TCP binary transport. A secure source also requires the
  server certificate to equal the pinned DER bytes.
- **Session.** CreateSession must repeat the discovered endpoint set; otherwise
  the collector suspects a downgrade and stops. It verifies the server's
  session signature over its certificate and nonce, and signs ActivateSession
  in turn.
- **Passwords.** A UserName password is encrypted with RSA-OAEP using the
  pinned server certificate and the session nonce. If the server's token policy
  is None, the collector refuses to send the password unless
  `allow_plaintext_password` is `true`.
- **Security tokens** are renewed at 75% of their revised lifetime.
- **Subscriptions.**
  - Subscriptions keep one Publish request outstanding and acknowledge each
    notification message.
  - `BadNoSubscription`, `BadSubscriptionIdInvalid` or a
    StatusChangeNotification recreates the subscription on the same session.
  - A lost session or channel reconnects from discovery.
  - Polled points use Read on a separate thread of the same session.
- **Decoding** is bounded: array length 10000, nesting depth 8, message size
  4 MiB, chunk count 128. A response that cannot be decoded closes the channel.

**Bounds, backpressure and reconnects.**

- **The buffer.** All sources share one bounded buffer. A single writer appends
  messages to the stream in order. A record leaves the buffer only after the
  daemon acknowledges its append.
- **Failed appends.** `full`, `unavailable`, a missing socket or a timeout are
  retried with backoff from `retry_min_ms` to `retry_max_ms`.
- **When the buffer cannot take a whole poll.** A Modbus poll is skipped rather
  than queued. An OPC UA source withholds its next Publish request, so the
  server's bounded queues absorb the excess and mark overflow. If the pause
  outlasts the subscription lifetime, the source resubscribes when space
  returns.
- **Failed OPC UA Reads.** A ServiceFault or an unanswered Read records each
  polled point as BAD with the status (for example `BadTimeout`) and counts
  `poll_failures`; polling continues on the next interval while the session and
  subscription stay up. A malformed Read answer closes the session, which then
  reconnects and resumes both polling and the subscription.
- **Gap records.** Skipped cycles, polls that overran their schedule and records
  dropped when another source took the space are reported in a `gap` record.
- **Reconnect backoff.** It doubles from `reconnect_min_ms` to
  `reconnect_max_ms`, and resets after a successful connection.
- **Losses.** Records still in the buffer are lost only if the collector process
  itself dies, or if the five-second `SIGTERM` flush runs out (the exit status
  is then 1). A daemon restart loses none.

## Simulators

`python3 -m industrial.simulator` (from `scripts/`) serves a Modbus TCP and an
OPC UA endpoint on loopback, whose values change only on ticks. Tests use the
classes directly:

- **`modbus.Simulator`** (TCP or TLS) and **`modbus.RtuSimulator`** (on a
  pseudo-terminal) have explicit register and bit values and per-address
  exception codes. One-shot faults: wrong transaction or unit, bad length or
  byte count, garbage, close, stall, bad CRC.
- **`opcua_server.Simulator`** has configurable endpoints (None,
  Basic256Sha256 Sign and SignAndEncrypt), trusted client certificates, users,
  and anonymous access. It implements subscriptions with keep-alive and
  lifetime counters, bounded queues with overflow bits, and per-node status
  codes and timestamps. Faults: `drop_connections()`,
  `expire_subscriptions()`, `restart()`, and corrupt chunks or bodies.

They are test fixtures, not devices or general servers.

## Reviewed upstream references

Reviewed on 2026-10-19, before the implementation. These are specification
references, not code sources. Nothing was copied from AWS or from any OPC UA or
Modbus implementation.

| Reference | Exact version | Use |
|---|---|---|
| MODBUS Application Protocol Specification | V1.1b3, 2012-04-26 (PDF SHA-256 `f80d0d71…ad2eb1`) | PDUs, function codes 01-04, quantity limits, exceptions |
| MODBUS Messaging on TCP/IP Implementation Guide | V1.0b, 2006-10-24 (`065d7117…b6f669`) | MBAP header, unit identifier |
| MODBUS over Serial Line Specification and Implementation Guide | V1.02, 2006-12-20 (`85a91e27…bdf7e5`) | RTU framing, CRC-16, inter-frame silence |
| MODBUS/TCP Security | Named by the Modbus Organization; the document itself was not retrievable (the download returned HTML) | Only TLS mutual authentication on port 802 is implemented; role-based authorization from certificate extensions is not |
| OPC 10000-6 Mappings | 1.05.07, 2026-04-15 (current at [reference.opcfoundation.org](https://reference.opcfoundation.org/Core/Part6/v105/docs/)) | UA Binary, UA TCP, Secure Conversation, Basic256Sha256 key derivation and padding |
| OPC 10000-4 Services, OPC 10000-7 Profiles | Release 1.05 | Service parameters, subscription state machine, the Basic256Sha256 algorithm suite |
| [AWS Modbus-RTU protocol adapter](https://docs.aws.amazon.com/greengrass/v2/developerguide/modbus-rtu-protocol-adapter-component.html) | `aws.greengrass.Modbus` 2.1.11 | Functional scope only (the Lambda is under the Greengrass Core Software License Agreement) |
| [SiteWise Edge OPC UA source](https://docs.aws.amazon.com/iot-sitewise/latest/userguide/configure-opcua-source.html) | Collector capability namespaces `iotsitewise:opcuacollector:2` and `:3` | Policy and mode pairing, Anonymous or Username identity, subscribe or poll, quality and timestamps |

The full SHA-256 values of the fetched Modbus documents:

```
f80d0d7132fd035104c3f21b01026470e3310ca3d8d493d9a9737d0d32ad2eb1  Modbus_Application_Protocol_V1_1b3
065d71170475642e82f370c648bf5135263cb20f61ce92c921add975fab6f669  Modbus_Messaging_Implementation_Guide_V1_0b
85a91e278b26d68c94a2f61a5300cd39c9e4c855034833f391abc6541fbdf7e5  Modbus_over_serial_line_V1_02
```

## Differences from the AWS components

- **Collection model.** The AWS Modbus adapter is a request/response Lambda on
  local pub/sub topics. It also writes (function codes 05, 06, 15, 16, 22, 23)
  and runs over serial RTU only. This collector polls configured points into a
  durable stream, reads only, and supports TCP and TLS as well as RTU. The AWS
  topics, payloads and recipe parameters are not provided.
- **SiteWise options.** The SiteWise collector adds node-filter browsing,
  property groups, deadbands, data-change triggers and other security policies
  (`AES128_SHA256_RSAOAEP`, `AES256_SHA256_RSAPSS`). This collector subscribes
  to or polls explicit NodeIds with policy None or Basic256Sha256 only.
- **Trust.** Server certificates are pinned exactly: there is no `TrustAny`
  setting and no CA chain validation for OPC UA.
- **SiteWise processing and publishing** (aliases, transformations, SiteWise
  ingestion) remain SF-071.

## Evidence

| Command | Covers |
|---|---|
| `make test-industrial` | 30 tests (`scripts/test_industrial.py`) with the pinned `cryptography` 44.0.3. **Vectors:** OPC UA NodeId, DateTime, DataValue and message encodings; bounded decoding of malformed input; the TLS 1.2 P_SHA256 test vector; Modbus CRC and V1.1b3 examples; all 53 configuration cases; secret-file modes; the CLI version check. **Collector against the simulators over real sockets and pseudo-terminals:** all Modbus types and word orders; exception codes once, then recovery; seven malformed or stalled response faults, each closing and reconnecting; non-finite floats; outage with bounded backoff; TLS mutual authentication with untrusted client, untrusted server, wrong host name and plaintext all refused; RTU with a bad CRC. **OPC UA:** subscription and poll interoperability; UNCERTAIN and BAD quality; exact source timestamps; type mismatch, non-finite, too-large and array values; corrupt chunks or bodies reconnect; disconnect; server-side subscription loss resubscribing on the same session; session loss; withheld Publish with overflow and gap records; the notification bound; Read ServiceFaults and timeouts reported BAD while polling resumes on a healthy session, and a malformed Read answer reconnecting; a full-buffer poll group making progress. **OPC UA security:** Basic256Sha256 Sign and SignAndEncrypt with UserName; wrong password, untrusted client certificate and wrong server pin all rejected; refusal to downgrade to policy None or to send a plaintext password |
| `make test-industrial-daemon` | **Setup:** the built zip application runs as its own component UID against the real daemon (debug build), with Modbus TCP and OPC UA Basic256Sha256 SignAndEncrypt + UserName simulators. **Export:** records reach the declared File stream and are exported in signed batches, which the SF-034 receiver checks independently. **`full` backpressure:** the buffer stays bounded, polls are skipped and Publish is withheld, then gap records follow. **Daemon kill -9:** nothing is lost. **Collector `SIGTERM`:** the buffer is flushed (exit 0). **No loss:** record identifiers are contiguous for every collector boot. Receipt: [sf070-industrial.json](validation/sf070-industrial.json) |
| `python3 scripts/test_industrial_interop.py --reference-site DIR` | **Cross-implementation, host-run.** The collector against an asyncua 1.1.5 server (policy None/anonymous, and Basic256Sha256 SignAndEncrypt/UserName, with a wrong password rejected) and a pymodbus 3.8.6 server. An asyncua client against the OPC UA simulator (Read, subscription, DeleteSubscriptions, wrong password), and a pymodbus client against the Modbus simulator. The references are test-only (asyncua LGPL-3.0, pymodbus BSD-3-Clause) |

**Not covered:**

- physical Pi, real serial hardware and RS-485 timing (physical acceptance stays
  in [#32](https://github.com/tyler-suifleet/suifleet/issues/32));
- interoperability with commercial PLCs or OPC UA servers beyond the two
  open-source references;
- live AWS, SiteWise or any AWS account;
- other OPC UA security policies, X.509 user identity tokens, certificate
  chains and revocation, browse and node filters, events and alarms, deadbands;
- Modbus writes and MODBUS/TCP Security role authorization;
- throughput and performance limits on target hardware.
