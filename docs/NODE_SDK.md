# TypeScript/Node component SDK (native IPC v1)

SF-302 adds `@suifleet/component-ipc` in [`sdk/typescript`](../sdk/typescript), a
TypeScript client for the native [component IPC v1](COMPONENT_IPC.md) protocol.
It complements the standard-library [Python SDK](../sdk/python/suifleet_ipc.py)
and speaks the same wire contract. It is a native SuiFleet interface, **not** AWS
Greengrass IPC wire, `aws-iot-device-sdk-js-v2` or Greengrass CLI compatibility;
porting an AWS component means calling these operations instead.

## Supported versions and platforms

| Item | Supported | Evidence |
|---|---|---|
| Node.js | 20 and newer (ESM only; `engines.node >=20.0.0`) | CI runs both suites on the pinned dev image's Node 22.22.2. Before review both suites also ran locally on Node 20.20.2 and the unit suite on Node 24.21.0 (see [validation](#validation)); CI does not repeat those versions |
| TypeScript consumers | Bundled `.d.ts`, built with strict TypeScript 5.9.3 | `tsc` with `strict`, `exactOptionalPropertyTypes`, `noUncheckedIndexedAccess` |
| Daemon protocol | IPC v1 (`version: 1`) | [`interfaces/ipc-v1.json`](../interfaces/ipc-v1.json) checked against the daemon, Python SDK and this SDK |
| Linux | x86-64 tested against the real daemon | `scripts/test_ipc_node.py` |
| macOS | Daemon offers the same socket protocol ([MACOS_RUNTIME.md](MACOS_RUNTIME.md#ipc-authentication)); SDK not yet tested there | none |
| Windows | Not supported: the [Windows runtime](WINDOWS_RUNTIME.md) has its own named-pipe operator interface, not the Unix-socket component IPC v1 | — |

The runtime package has no third-party dependencies (only `node:net`,
`node:crypto`, `node:util`, `node:readline` in tests). It does not run on the
daemon user: provision each IPC client as its own platform-managed Unix user
exactly as for the Python SDK; shared daemon-UID components are denied.

## Distribution and provenance

The package is not published to the npm registry yet; `package.json` is marked
`private` so it cannot be published by accident. A public registry release needs
a reviewed release workflow with registry provenance attestation and an owned
npm scope; that remains open work. Today it is distributed as source in this
repository and as a reproducible tarball built from a reviewed commit:

```sh
make sdk-node          # locked pnpm install, strict tsc, npm pack; prints the tarball SHA-256
npm install ./sdk/typescript/suifleet-component-ipc-1.0.0.tgz
```

Provenance is the reviewed git commit plus the printed SHA-256 of the tarball:
the build uses only the committed `pnpm-lock.yaml` (TypeScript 5.9.3 and
`@types/node` 20.19.39, both build-time only) and the pinned dev image's
Node/pnpm/npm. `scripts/pack_sdk_node.sh` deletes `dist/`, compiles it again and
packs from a private staging copy with fixed modes. Every file is 0644, except
the runnable examples (0755). `npm pack` already normalizes mtimes and owners
but keeps input modes, so this staging step means checkout permissions and
umask do not change the hash. Verifying a supplied tarball needs only its
`sha256sum` against the release record; rebuilding needs the reviewed commit
and the pinned toolchain. CI builds the same tarball in `make ci` (`test-sdk-node`) and runs
the real-daemon suite against that packed artifact, not against the source
tree. Vendoring `dist/` from an unreviewed build or installing from an
unverified tarball is outside this boundary.

## API

```ts
import { Client, IpcError, TransportError, OutboundBuffer } from '@suifleet/component-ipc';

const client = new Client({
  socketPath: '/run/suifleet/ipc.sock', // default
  component: 'sensor',                  // configured name owned by this UID ('' for administrators)
  timeoutMs: 120_000,                    // one deadline per request, including encoding and connect retries/backoff
  connectAttempts: 1,                    // >1 retries only failures before any byte was sent
  connectRetryDelayMs: 250,              // doubled up to 5 s
});
```

The deadline starts when a typed helper is called, so reading caller-supplied
options and payloads counts against it. The deadline is also checked when the
response completes: a reply whose data callback runs after the deadline (for
example after an event-loop stall) is never accepted, and the request fails as a
`TransportError` with outcome `unknown`, without replay, because it was sent.
`putData` sends one request per chunk and
each chunk has its own `timeoutMs` deadline; a whole upload may take longer.

`timeoutMs` must be a finite number in (0, 2147483647] and `connectRetryDelayMs`
a finite number in [0, 2147483647]: Node timers fire after 1 ms for larger or
non-finite delays, so the constructor throws `RangeError` instead of creating a
client that would abandon healthy requests. `connectAttempts` must be a positive
integer.

| Area | Methods (wire operation) |
|---|---|
| Generic | `request(operation, {component, topic, payload, signal})` |
| Configuration and health | `configuration()` (`config`; `null` before the component has a committed configuration), `health(state)` |
| Management | `status()`, `start()`, `stop()`, `restart()`, `logs()`; administrators: `install(id)`, `list()` |
| Volatile and durable pub/sub ([MESSAGING.md](MESSAGING.md)) | `publish(topic, payload)`, `receive(topic)`, `subscribe(filter, qos)`, `unsubscribe(filter)`, `ack(filter, id)`, `messages(filter, { qos })` async iterator; administrators: `messaging()` |
| Durable streams ([STREAMS.md](STREAMS.md)) | `streamCreate`, `streamAppend`, `streamRead`, `streamDescribe`, `streamDelete`, `streamList`; trusted exporters: `streamExportAck`, `streamExportStatus` |
| Shadows ([SHADOWS.md](SHADOWS.md)) | `shadowGet`, `shadowUpdate(name, state, {version, clientToken})`, `shadowDelete`, `shadowList`; administrators: `shadows()` |
| Opaque data ([OPAQUE_DATA.md](OPAQUE_DATA.md)) | `putData(stream, key, bytes, options)` — same chunk geometry and descriptor as the Python SDK. `options.metadata` is a string-to-string map (`DataMetadata`), as the daemon requires: at most 16 entries, keys of 1–64 UTF-8 bytes, values of at most 256 UTF-8 bytes, at most 6144 serialized bytes. `validateDataMetadata` checks it; the CLI's `--metadata` takes the same JSON object |
| Client devices ([CLIENT_DEVICES.md](CLIENT_DEVICES.md)) | administrators: `clientDevices(action, fields)` |
| CLI | `suifleet-ipc [--socket] [--component] [--topic] [--payload JSON] OPERATION`, `--file` for `data_put` (a regular file of at most 64 MiB; larger files, FIFOs, devices and directories are refused before reading or connecting) |

Option objects use camelCase; results are the daemon's wire objects (for
example `stream_describe` returns `max_size`). Response fields added by future
daemons are passed through and must be ignored by callers. Numbers are
JavaScript numbers: sequence numbers and message IDs above 2^53 would lose
precision (not reachable by the documented limits).

## Errors, outcomes and retries

| Error | Meaning | Safe reaction |
|---|---|---|
| `IpcError` (`code`, `operation`, `oldest`) | The daemon processed and refused the request. `request_failed` covers authorization, validation and lifecycle failures (details stay in daemon logs); publish backpressure returns `full`; streams/shadows return protocol codes such as `full`, `evicted`, `exists`, `not-found`, `conflict`, `quota`. | Denied requests and coded publish/stream/shadow refusals stored nothing. A generic `request_failed` from a mutation is not a guarantee: a `stream_append` can return it after the record was stored (for example when a later segment cleanup fails), so check the stream's `newest` before resending; a `publish` can return it when a durable spool rollback failed, and the surviving copy is delivered after a daemon restart. Fix the ACL or request; for `full`/backpressure retry later. |
| `TransportError` with `outcome: 'not_sent'` | No request byte reached the daemon (missing socket during a restart, refused connection, abort before connect or during `connectAttempts` backoff, where the abort reason is the `cause`; or `timeoutMs` expiring before the request was written, even if the connection had just completed). | Retrying is safe for every operation. `connectAttempts` does this automatically for ENOENT/ECONNREFUSED/EAGAIN. |
| `TransportError` with `outcome: 'unknown'` | The request may have executed: timeout, closed connection, truncated response. An unmapped UID also sees a closed connection. | Never retry a mutation blindly. Inspect status, stream `newest`, shadow version or the deployment outcome first. |
| `ProtocolError` (a `TransportError`, outcome `unknown`) | Unsupported response version, malformed or oversize frame, including a failure envelope (`ok: false`) without a nonempty string `error` or with a non-integer `oldest`. Such a reply is never treated as a definite refusal. | As above; check SDK/daemon versions. |
| `DataPutError` (`upload`, `chunk`, `receipts`) | A chunk failed. | Resume with the same `upload` and `startChunk: chunk` and the same bytes. |
| `RangeError`, `TypeError` | Refused locally before sending: request frame > 64 KiB, publish payload > 4096 serialized bytes, data > 64 MiB, invalid options, `data_put` metadata that is not a string-to-string map within the daemon's limits (an explicit `null` is refused; only an omitted or `undefined` value defaults to `{}`). | Fix the input. |

The SDK never resends a request after a byte was sent. Each request uses its own
connection because v1 has no pipelining or streaming, so a daemon restart needs
no session recovery: the next request reconnects. Durable subscriptions,
streams and shadows persist across restarts in the daemon; volatile inboxes and
self-reported health do not.

`messages(filter, { qos })` polls `receive` while the queue is empty and keeps
polling across `not_sent` failures (daemon restarting) until its `signal`
aborts. `qos` is required and must be the QoS the subscription was created
with: the daemon returns the same message shape, including `id`, for QoS 0 and
QoS 1, so an `id` does not make a message durable and the SDK cannot infer
the QoS. A missing or invalid `qos` rejects the first `next()` with
`RangeError` before any `receive`. Each delivery has `message`, `durable` and
`ack()`. With `qos: 1`, a subscription message is `durable`: acknowledge it
before pulling the next one; an unacknowledged message is delivered again,
including after a restart (at-least-once). With `qos: 0`, and for volatile
inbox messages (no `id`; pass `qos: 0` for a legacy inbox topic), reads are
destructive, `durable` is false and `ack()` sends nothing and returns true, so
a QoS 0 reader needs no `ack` operation in its ACL. Passing `qos: 1` for a
QoS 0 subscription sends an `ack` the daemon cannot honor; a reader without
the `ack` operation sees that `request_failed` error from `ack()`. Aborting the `signal` while the
iterator is idle or before a `receive` is sent ends the iteration normally.
Aborting while a `receive` is in flight throws that request's `TransportError`
with outcome `unknown` instead, because the daemon may already have removed a
volatile or QoS 0 message from the queue.

`pollIntervalMs` (default 500) and `maxReconnectDelayMs` (default 5000) must be
finite numbers in [0, 2147483647]. The first `next()` rejects anything else with
`RangeError` before sending a `receive`, so an out-of-range value can never
become Node's 1 ms fallback and poll a restarting or idle daemon in a tight
loop. Reconnect backoff starts at the smaller of `pollIntervalMs` and
`maxReconnectDelayMs` and doubles up to `maxReconnectDelayMs`, so a long idle
poll interval never delays reconnecting to a restarted daemon past the ceiling.

Only an omitted (`undefined`) numeric option takes its default. An explicit
`null` is refused with `RangeError` like any other invalid value; this applies
to the iterator timers, the `Client` timer/retry options, `OutboundBuffer`
`maxItems`/`maxBytes` and the `flush` `maxAttempts`/`retryDelayMs`.

### Bounded buffering

`OutboundBuffer` queues `publish` and `stream_append` items in process memory,
bounded by `maxItems` (default 256) and `maxBytes` (default 1 MiB); a full
buffer throws `BufferFullError` instead of growing or dropping. `flush()` sends
in order and never drops or reorders:

- a `publish` `IpcError` `full` (inbox full, durable subscription queue
  limit) is retried up to `maxAttempts`: the daemon returns `full` only when
  nothing was stored;
- a `stream_append` refusal with a stream code stored nothing. `full` and
  `unavailable` are retried up to `maxAttempts`; permanent codes (`invalid`,
  `retired`, `not-found`, ...) end the flush at once with `rejected: true`;
  the item stays queued until the caller drops it with `discardHead()`, which
  lets the items behind it continue;
- a `not_sent` failure is retried up to `maxAttempts`;
- `maxAttempts` must be a positive integer and `retryDelayMs` a finite number
  in [0, 2147483647]; `flush` rejects anything else with `RangeError` before
  sending, so `NaN` or `Infinity` can never bypass the retry cap;
- when retries run out, `flush` returns with the item still queued and the
  last error;
- aborting the `flush({signal})` signal rejects with the abort reason at once,
  including when the abort lands inside the client's own `connectAttempts`
  backoff; the unsent head stays queued and the next `flush` can start
  immediately;
- an `unknown` outcome, a `stream_append` `request_failed` (which may follow
  a stored record), or any `publish` `IpcError` other than `full` stops the
  buffer. A generic publish `request_failed` is not a refusal guarantee: when
  the durable spool cannot roll back a failed commit, a surviving copy is
  delivered after the daemon restarts. Daemons older than the `full` publish
  code return `request_failed` for backpressure, so with them publish
  backpressure also stops the buffer. Such a failure stops the buffer with `UncertainDeliveryError`; the caller
  checks the destination and chooses `resolveUncertain('retry')` (may
  duplicate) or `'discard'` (may lose). Any other value, including an omitted
  argument from plain JavaScript, throws `TypeError` and leaves the buffer
  blocked with its queue unchanged.

Each payload is serialized and copied (frozen) when it is queued, so changing
the caller's object afterwards changes neither the queued message nor the byte
accounting. The queued item itself is frozen too, so inspecting `head` (or the
item on `UncertainDeliveryError`) cannot change its destination or payload.
An item whose request frame would exceed the 64 KiB IPC frame limit is
refused with `RangeError` at enqueue, leaving the queue unchanged, so no
unsendable item can block the items behind it. `discardHead()` refuses an
uncertain head: that choice always goes through `resolveUncertain`.

The buffer does not survive the Node process. Data that must survive belongs in
a durable stream.

## Examples

[`sdk/typescript/examples`](../sdk/typescript/examples) contains runnable
components that the real-daemon test executes unchanged under their own UIDs:

- `sensor.mjs`: configuration, health, durable stream and topic announcements
  through an `OutboundBuffer`.
- `consumer.mjs`: durable QoS 1 subscription with acknowledgement after processing.
- `shadow-reporter.mjs`: follows a named shadow's desired state through delta
  notifications and reports it with conditional (versioned) updates.

Each file header lists the ACL it needs. A matching configuration is in
[`scripts/test_ipc_node.py`](../scripts/test_ipc_node.py).

## Compatibility rules

The SDK sends protocol `version: 1` only and rejects responses of any other
version. Operation names, request fields and limits are recorded in
[`interfaces/ipc-v1.json`](../interfaces/ipc-v1.json);
`scripts/tests/test_ipc_interface.py` fails if the daemon's operation list,
administrator-only set or stream/shadow codes, the Python SDK or this SDK
diverge from it. A wire-incompatible change requires a new protocol version and
a new SDK major version, with old-client/new-server tests, as required by
[COMPONENT_IPC.md](COMPONENT_IPC.md#wire-contract-and-compatibility). IPC never
reboots the platform or asserts firmware/boot health; platform tools own those.

## Validation

| Command | What it runs |
|---|---|
| `make test-sdk-node` | Locked install, strict build and pack; `scripts/tests/test_ipc_interface.py` (daemon/Python/TypeScript contract); `node --test test/client.test.mjs` against a local fake socket peer: exact frames, every outcome class, split/oversize/malformed/future-version responses, no resend after send, connect retry across a late listener, one `timeoutMs` deadline spanning backoff (no dispatch after expiry, including a connect callback delayed past it by an event-loop stall and payload serialization that consumes the budget before the first connect, through `request`, `publish`, `streamCreate`, `streamRead` and `shadowUpdate`; remaining-time response wait; a complete response dispatched after the deadline by an event-loop stall rejected as `unknown` and not replayed), abort, chunking/resume, `data_put` metadata accepted as a string map and refused locally (never sent) for non-string values or limit violations, including through the CLI, iterator ack (QoS 1 acks subscription messages by id but never a legacy inbox message; QoS 0 deliveries of the daemon's id-bearing shape are not durable and send no `ack`; missing/invalid `qos` refused before any `receive`), iterator abort (in-flight `receive` throws `unknown`; idle or unsent abort ends cleanly), buffer bounds/order/uncertain stop, no replay of a `stream_append` `request_failed` or of a `publish` failure other than `full` (including after a daemon restart), transient/permanent stream codes, payload snapshot across a retry, prompt `flush` abort during a nested client connect retry, `null` configuration, `timeoutMs`/`connectRetryDelayMs`/`connectAttempts` timer-range boundaries (2147483647 ms honored, larger/non-finite refused without a `TimeoutOverflowWarning`), invalid `maxAttempts`/`retryDelayMs` refused before any send, `messages()` `pollIntervalMs`/`maxReconnectDelayMs` refused before any `receive` and the 2147483647 ms maximum honored for both polling and reconnect backoff, reconnect within `maxReconnectDelayMs` when `pollIntervalMs` is longer (late listener after the first failure and after a successful receive), explicit `null` refused for every numeric timer/retry/bound option. |
| `make test-ipc-node` | `scripts/test_ipc_node.py --package <tarball>`: the packed SDK and its unmodified examples against the real debug daemon with local chain/Walrus simulators, as distinct real Unix UIDs in a disposable root, network-none container. Administrator install/list/status/logs; `null` configuration before the first install handled by the sensor example; configuration, health, streams, QoS 1 pub/sub with ack; Python↔Node interoperability on one daemon; default-deny for admin-only, cross-component, topic, stream, shadow-section and spoofed-name requests without side effects, unmapped UID; bounded buffering against the 32-message volatile inbox, the durable spool limit and a full `RejectNewData` stream; reconnect across a daemon restart with redelivery of the unacknowledged message; a QoS 0 `messages({ qos: 0 })` reader whose ACL lacks `ack` receives the daemon's id-bearing deliveries as non-durable, sends no `ack` and leaves its queue empty while the QoS 1 copies of the same publications still wait for `ack`, and a mismatched `qos: 1` iterator surfaces the `ack` refusal; shadow deltas with conditional updates, `conflict` and `not-found`; `data_put` refusal with resume information. |

Fake-peer unit tests establish client framing and classification only; the
behavioral claims above come from the real-daemon suite. Not covered: macOS and
32-bit/ARM targets, opaque-data custody end to end through this SDK (refusal
only; custody is covered for the Python SDK by `make test-opaque`), the
client-device and stream-exporter operations beyond the shared contract check,
physical devices ([SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32))
and public registry publication. No public-network, mainnet or production
provider result is implied.
