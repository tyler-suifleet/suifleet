# Native component IPC and SDK (v1)

SF-027 provides an opt-in Unix-stream API (Linux, and macOS since SF-075; see
[MACOS_RUNTIME.md](MACOS_RUNTIME.md#ipc-authentication)) and a dependency-free Python 3.10+
SDK/CLI in [`sdk/python/suifleet_ipc.py`](../sdk/python/suifleet_ipc.py). This is a
native SuiFleet interface, not AWS Greengrass IPC, SDK or CLI compatibility.

## Identity and provisioning

Omitting `[ipc]` disables the listener. The operator creates the socket's parent
before startup; it must be daemon-owned and have no group/world write permission.
Use an absolute path in a traversable directory (0755), outside component workspaces.
The socket is 0666 so separately provisioned Unix users can connect. Every request
is authorized using the kernel's Unix peer UID; no token, claimed name, environment
variable or JSON caller field authenticates a client. Unknown fields are rejected.
A preexisting socket/file is never removed on startup. Stop the old service and
inspect/remove stale sockets before restarting after SIGKILL.

```toml
[ipc]
socket = "/run/suifleet/ipc.sock"
administrators = [0]

[ipc.components.sensor]
uid = 2001
operations = ["config", "status", "health", "publish", "receive"]
publish = ["sensor/events"]
subscribe = ["sensor/commands"]
```

For the shipped `ProtectSystem=strict` unit, provision a systemd drop-in before
using `/run/suifleet/ipc.sock` (then reload units and restart during a maintenance
window). `RuntimeDirectory` creates the daemon-owned, writable runtime path;
configuration alone does not create it:

```ini
[Service]
RuntimeDirectory=suifleet
RuntimeDirectoryMode=0755
```

Each component principal needs a distinct, non-root Unix UID, different from the
daemon and all administrators. ACLs are operator-owned daemon configuration, not
recipe-supplied privileges. Components can address only their exact configured
name and operations; topics are exact strings, without wildcards. Component ACLs
cannot grant `install` or `list`. Operators explicitly listed as administrators
can manage all components and access their logs/public configuration.

The shipped supervisor still runs components as the daemon user. **Those shared-UID
processes are denied component IPC and cannot be made administrators.** Provision
IPC clients as separate platform-managed Unix services/users with access only to
the socket, not daemon keys/state/configuration or sibling credentials. This ticket
does not introduce per-component Unix execution users or turn existing shared-user
supervision into an isolation boundary. The daemon user, root, operator ACL file
and daemon-owned socket directory are trusted infrastructure; running untrusted
code as that user compromises this boundary, including socket integrity. Do not
use bearer credentials to pretend to isolate same-UID components. The UID mapping
is a local platform identity; it is not a Sui account or cloud authorization grant.

At most 64 component principals, 16 administrators, 128 total subscriptions and
128 publication topics per component are accepted. Validate configuration before
starting the daemon. Reload ACLs by restarting it; existing IPC connections then
end. Provisioning Unix accounts and directory access remains the platform's job;
no keys, user accounts or enrollment settings are embedded by package builds.

## Operations

| Operation | Request | Result and boundary |
|---|---|---|
| `install` | `payload` = on-chain deployment ID | Administrator only. Revalidates manifest/cap, live target/operator, record/recipe/artifact authentication and admission controls through the existing install path. No arbitrary local shell/artifact install. Existing outcome is returned without replay. Desired-state and daemon-self-update records stay with reconciliation. |
| `list` | no component needed | Administrator only. Installed journal entries and supervised runtime statuses. |
| `start`, `stop`, `restart` | `component` | Controls only an already committed supervised version. Start waits for readiness and requires dependencies Running. Stop/restart refuses while a committed dependent is supervised. |
| `status` | `component` | Installed record, supervisor runtime status and separately identified self-reported health, or nulls when unknown. |
| `logs` | `component` | Last 8 KiB of the committed supervised component's private log, UTF-8 with replacement. No arbitrary paths or log creation. |
| `config` | `component` | Public committed configuration record, including version/deployment/effective/defaults. Confidential device-overlay values are never returned. Poll the record to observe configuration changes. |
| `health` | `component`, `payload` = `healthy`, `degraded` or `broken` | Volatile self-reported health plus server timestamp. Does not assert firmware boot health, change supervisor phase or override signed fleet telemetry. Principal must exist in the configured ACL. |
| `publish` | `component`, exact `topic`, JSON `payload` | Fanout to configured subscribers, message envelope includes authenticated component name (or `@operator/UID` for administrators). Payload <=4096 serialized bytes. Atomic backpressure if any inbox is full. |
| `receive` | `component`, exact `topic` | Destructive nonblocking dequeue, or null. Each principal/topic has 32 messages maximum. With a durable subscription to that filter, its oldest message instead ([MESSAGING.md](MESSAGING.md)). |
| `stream_create`, `stream_delete`, `stream_append`, `stream_read`, `stream_describe` | `component`, stream name as `topic` | Durable streams (SF-034, [STREAMS.md](STREAMS.md)); need `[streams]` and a matching `streams` glob in the component's ACL. |
| `stream_list` | `component` | Stream names the caller's `streams` globs match. |
| `subscribe`, `unsubscribe`, `ack` | `component`, filter `topic` | Durable pub/sub (SF-032); need `[pubsub]` and a `SubscribeToTopic` policy. |
| `shadow_get`, `shadow_update`, `shadow_delete` | `component`, shadow name as `topic` (`""` is classic) | Device shadows (SF-033, [SHADOWS.md](SHADOWS.md)); need `[shadows]` and a matching `shadow_policies` grant. |
| `shadow_list` | `component` | Named shadows a `ListNamedShadowsForThing` grant matches. |
| `shadows` | no component needed | Administrator only. Shadow versions, sync and notification counters. |
| `messaging` | no component needed | Administrator only. Durable subscription and bridge counters. |

Stops are runtime controls: explicit start, replacement deployment or daemon restart
may start the committed version again. Stop does not uninstall or alter chain desired
state. Local config writes go through a new authenticated configuration deployment
and `install`, preserving SF-015 validation/rollback semantics. No silent local edit
of authoritative configuration or independent second installation journal exists.
Install `ok: true` means the request was processed: inspect `status`/`outcome` for
Applied (1), Failed (2), pending or deferred admission; a digest is not success.

The `publish`/`receive` inboxes above are bounded, volatile and non-durable.
Durable subscriptions with MQTT filters, QoS 1 acknowledgement, AWS-shaped
`pubsub_policies` and the local MQTT bridge are opt-in with `[pubsub]`: see
[MESSAGING.md](MESSAGING.md) (SF-032). Health and volatile inboxes clear on daemon restart; runtime and installed evidence comes
from the existing supervisor/journal. IPC does not reboot the platform.

## SDK and CLI

```python
from suifleet_ipc import Client
client = Client('/run/suifleet/ipc.sock', component='sensor')
configuration = client.configuration()
client.health('healthy')
client.publish('sensor/events', {'temperature': 21})
command = client.receive('sensor/commands')
```

```sh
python3 sdk/python/suifleet_ipc.py --socket /run/suifleet/ipc.sock list
python3 sdk/python/suifleet_ipc.py --component sensor status
python3 sdk/python/suifleet_ipc.py --component sensor restart
python3 sdk/python/suifleet_ipc.py install --payload '"0xDEPLOYMENT_ID"'
```

The Python module provides named methods for all workflows and generic `request`.
Transport exceptions/timeouts have an **unknown mutation outcome**. Never automatically
retry a destructive operation; inspect `status`, logs and the durable deployment
outcome before deciding. The SDK does not retry. Only install has existing-deployment
replay protection. The server drops timed-out queued requests before execution, but
disconnecting does not revoke an accepted request: it may still execute or finish.

## Wire contract and compatibility

One UTF-8 JSON object plus LF per connection, at most 65536 bytes including LF.
Requests have `version: 1`, `operation`, optional `component`/`topic` (default empty)
and optional `payload` (default null). No pipelining or streaming. Responses contain
`version: 1`, `ok` and either `data` or `error: "request_failed"` (stream and shadow
operations return their protocol codes instead, such as `full`, `evicted` or `conflict`). Errors deliberately
do not disclose private command/configuration diagnostics; operators inspect daemon
logs. A denied/invalid/future-version request has no handler side effects.

The server rejects unmapped kernel UIDs before allocating client slots. It caps
simultaneous clients at 32 globally and four per UID, queued work at 32, request
framing at three seconds and total connection lifetime at 120 seconds.
State-changing requests run serially between poll cycles;
they do not interrupt an in-flight install. Long chain/bootstrap/updater operations
can delay local calls. No availability SLA or real-time lifecycle response is promised.
Oversize responses are rejected, never silently truncated (except the specified
log tail). Python clients must keep their own timeout suitable for the workload.

Version 1 operation names, request field meanings and successful response fields
are stable. Clients ignore added response fields. New operations may be added;
unknown operations fail closed. Incompatible request/response semantics or removal
require a new protocol version; future request versions are rejected even for admins.
SDK releases must retain this wire version explicitly and test old-client/new-server
compatibility before changing it. Recipe/chain protocol versions are independent.

## Verification and remaining acceptance

Rust IPC tests cover UID policy, default denial, cross-component/topic negatives,
spoofed caller fields, future versions, bounded frames, socket lifecycle, health and
atomic inbox backpressure. `scripts/test_ipc.py` exercises the real daemon and SDK
against local gRPC/Walrus simulators, using real distinct Unix UIDs in a disposable
root container for authorization tests. It covers every local-management workflow,
wrong-target and digest rejection, no install replay, messaging and cleanup. Idle
floods from unmapped and mapped UIDs must preserve administrator access, and
request timeouts must release principal slots. Control-character log tails must
remain within the response frame. Run `make test-ipc` as root when already inside
a disposable container; the host target starts its own root container.
Physical Pi/target integration remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32);
no physical or public-network result is implied by simulator acceptance.

SF-057 adds a separate [scoped AWS credential adapter](AWS_CREDENTIALS.md) using
the same distinct-UID provisioning boundary. Native IPC ACLs do not implicitly
grant AWS credentials; its credential socket has an explicit operator grant list.
Shared-daemon-UID components remain denied.
