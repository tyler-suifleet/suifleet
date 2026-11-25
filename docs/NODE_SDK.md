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
Node/pnpm. CI builds the same tarball in `make ci` (`test-sdk-node`) and runs
the real-daemon suite against that packed artifact, not against the source
tree. Vendoring `dist/` from an unreviewed build or installing from an
unverified tarball is outside this boundary.

## API

```ts
import { Client, IpcError, TransportError, OutboundBuffer } from '@suifleet/component-ipc';

const client = new Client({
  socketPath: '/run/suifleet/ipc.sock', // default
  component: 'sensor',                  // configured name owned by this UID ('' for administrators)
  timeoutMs: 120_000,                    // whole request; the daemon ends connections at 120 s
  connectAttempts: 1,                    // >1 retries only failures before any byte was sent
  connectRetryDelayMs: 250,              // doubled up to 5 s
});
```

| Area | Methods (wire operation) |
|---|---|
| Generic | `request(operation, {component, topic, payload, signal})` |
| Configuration and health | `configuration()` (`config`; `null` before the component has a committed configuration), `health(state)` |
| Management | `status()`, `start()`, `stop()`, `restart()`, `logs()`; administrators: `install(id)`, `list()` |
| Volatile and durable pub/sub ([MESSAGING.md](MESSAGING.md)) | `publish(topic, payload)`, `receive(topic)`, `subscribe(filter, qos)`, `unsubscribe(filter)`, `ack(filter, id)`, `messages(filter, options)` async iterator; administrators: `messaging()` |
| Durable streams ([STREAMS.md](STREAMS.md)) | `streamCreate`, `streamAppend`, `streamRead`, `streamDescribe`, `streamDelete`, `streamList`; trusted exporters: `streamExportAck`, `streamExportStatus` |
| Shadows ([SHADOWS.md](SHADOWS.md)) | `shadowGet`, `shadowUpdate(name, state, {version, clientToken})`, `shadowDelete`, `shadowList`; administrators: `shadows()` |
| Opaque data ([OPAQUE_DATA.md](OPAQUE_DATA.md)) | `putData(stream, key, bytes, options)` — same chunk geometry and descriptor as the Python SDK |
| Client devices ([CLIENT_DEVICES.md](CLIENT_DEVICES.md)) | administrators: `clientDevices(action, fields)` |
| CLI | `suifleet-ipc [--socket] [--component] [--topic] [--payload JSON] OPERATION`, `--file` for `data_put` |

Option objects use camelCase; results are the daemon's wire objects (for
example `stream_describe` returns `max_size`). Response fields added by future
daemons are passed through and must be ignored by callers. Numbers are
JavaScript numbers: sequence numbers and message IDs above 2^53 would lose
precision (not reachable by the documented limits).

## Errors, outcomes and retries

| Error | Meaning | Safe reaction |
|---|---|---|
| `IpcError` (`code`, `operation`, `oldest`) | The daemon processed and refused the request. `request_failed` covers authorization, validation and lifecycle failures (details stay in daemon logs); streams/shadows return protocol codes such as `full`, `evicted`, `exists`, `not-found`, `conflict`, `quota`. | Denied requests and coded stream/shadow refusals stored nothing. A generic `request_failed` from a mutation is not a guarantee: a `stream_append` can return it after the record was stored (for example when a later segment cleanup fails), so check the stream's `newest` before resending. Fix the ACL or request; for `full`/backpressure retry later. |
| `TransportError` with `outcome: 'not_sent'` | No request byte reached the daemon (missing socket during a restart, refused connection, abort before connect). | Retrying is safe for every operation. `connectAttempts` does this automatically for ENOENT/ECONNREFUSED/EAGAIN. |
| `TransportError` with `outcome: 'unknown'` | The request may have executed: timeout, closed connection, truncated response. An unmapped UID also sees a closed connection. | Never retry a mutation blindly. Inspect status, stream `newest`, shadow version or the deployment outcome first. |
| `ProtocolError` (a `TransportError`, outcome `unknown`) | Unsupported response version, malformed or oversize frame. | As above; check SDK/daemon versions. |
| `DataPutError` (`upload`, `chunk`, `receipts`) | A chunk failed. | Resume with the same `upload` and `startChunk: chunk` and the same bytes. |
| `RangeError` | Refused locally before sending: request frame > 64 KiB, publish payload > 4096 serialized bytes, data > 64 MiB, invalid options. | Fix the input. |

The SDK never resends a request after a byte was sent. Each request uses its own
connection because v1 has no pipelining or streaming, so a daemon restart needs
no session recovery: the next request reconnects. Durable subscriptions,
streams and shadows persist across restarts in the daemon; volatile inboxes and
self-reported health do not.

`messages(filter)` polls `receive` while the queue is empty and keeps polling
across `not_sent` failures (daemon restarting) until its `signal` aborts. For
QoS 1, acknowledge each delivery before pulling the next one; an unacknowledged
message is delivered again, including after a restart (at-least-once). Volatile
inbox and QoS 0 reads are destructive.

### Bounded buffering

`OutboundBuffer` queues `publish` and `stream_append` items in process memory,
bounded by `maxItems` (default 256) and `maxBytes` (default 1 MiB); a full
buffer throws `BufferFullError` instead of growing or dropping. `flush()` sends
in order and never drops or reorders:

- a `publish` `IpcError` (inbox full, durable spool limit) is retried up to
  `maxAttempts`: publication is all-or-nothing, so a refused one stored nothing;
- a `stream_append` refusal with a stream code stored nothing. `full` and
  `unavailable` are retried up to `maxAttempts`; permanent codes (`invalid`,
  `retired`, `not-found`, ...) end the flush at once with `rejected: true`;
  the item stays queued until the caller drops it with `discardHead()`, which
  lets the items behind it continue;
- a `not_sent` failure is retried up to `maxAttempts`;
- when retries run out, `flush` returns with the item still queued and the
  last error;
- an `unknown` outcome, or a `stream_append` `request_failed` (which may follow
  a stored record), stops the buffer with `UncertainDeliveryError`; the caller
  checks the destination and chooses `resolveUncertain('retry')` (may
  duplicate) or `'discard'` (may lose).

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
| `make test-sdk-node` | Locked install, strict build and pack; `scripts/tests/test_ipc_interface.py` (daemon/Python/TypeScript contract); `node --test test/client.test.mjs` against a local fake socket peer: exact frames, every outcome class, split/oversize/malformed/future-version responses, no resend after send, connect retry across a late listener, abort, chunking/resume, iterator ack, buffer bounds/order/uncertain stop, no replay of a `stream_append` `request_failed`, transient/permanent stream codes, payload snapshot across a retry, `null` configuration. |
| `make test-ipc-node` | `scripts/test_ipc_node.py --package <tarball>`: the packed SDK and its unmodified examples against the real debug daemon with local chain/Walrus simulators, as distinct real Unix UIDs in a disposable root, network-none container. Administrator install/list/status/logs; `null` configuration before the first install handled by the sensor example; configuration, health, streams, QoS 1 pub/sub with ack; Python↔Node interoperability on one daemon; default-deny for admin-only, cross-component, topic, stream, shadow-section and spoofed-name requests without side effects, unmapped UID; bounded buffering against the 32-message volatile inbox, the durable spool limit and a full `RejectNewData` stream; reconnect across a daemon restart with redelivery of the unacknowledged message; shadow deltas with conditional updates, `conflict` and `not-found`; `data_put` refusal with resume information. |

Fake-peer unit tests establish client framing and classification only; the
behavioral claims above come from the real-daemon suite. Not covered: macOS and
32-bit/ARM targets, opaque-data custody end to end through this SDK (refusal
only; custody is covered for the Python SDK by `make test-opaque`), the
client-device and stream-exporter operations beyond the shared contract check,
physical devices ([SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32))
and public registry publication. No public-network, mainnet or production
provider result is implied.
