# Deployment service configuration (SF-081)

Recipe schema `1.3` adds an optional `services` section, version `1`. The
existing on-chain recipe SHA-256 authenticates every byte of this section.
Only the verified deployment root authorized by the fleet owner supplies
service configuration;
dependency recipes must omit it. The on-chain authority includes the owner and
accounts holding a delegated `DeployerRole`; it does not require a separate
owner signature on each recipe. Delegation therefore authorizes service
destinations and grants, but never authorizes reading arbitrary local files.
The root may grant access to itself and the
components actually present in its resolved dependency plan, including reused
components. Omitted soft dependencies cannot receive grants.

```json
{
  "services": {
    "version": 1,
    "grants": {
      "sensor": {
        "operations": ["config", "publish", "stream_append", "shadow_get"],
        "pubsub_policies": [{
          "operations": ["aws.greengrass#PublishToTopic"],
          "resources": ["sensors/*"]
        }],
        "streams": ["samples"],
        "shadow_policies": [{
          "operations": ["aws.greengrass#GetThingShadow"],
          "resources": ["$aws/things/device1/shadow/name/settings"]
        }]
      }
    },
    "pubsub": {
      "max_messages_per_subscription": 1000,
      "max_bytes_per_subscription": 4194304,
      "qos0_messages_per_subscription": 100,
      "bridge": {
        "broker": "127.0.0.1:1883",
        "password_file": "/etc/suifleet/mqtt-password",
        "mappings": [{
          "topic": "sensors/#",
          "source": "Pubsub",
          "target": "LocalMqtt",
          "target_topic_prefix": "device1/"
        }]
      }
    },
    "streams": {
      "max_streams": 32,
      "max_total_bytes": 67108864,
      "definitions": [{
        "name": "samples",
        "max_size": 1048576,
        "strategy_on_full": "RejectNewData",
        "persistence": "File",
        "export": {
          "url": "https://ingest.example/v1/streams",
          "bearer_token_file": "/etc/suifleet/export.token"
        }
      }]
    }
  }
}
```

This is a section of a native recipe, alongside the required component,
artifact, lifecycle and configuration fields; the full wire contract is
[recipe.schema.json](../interfaces/recipe.schema.json). Service fields use the
same snake_case names, defaults, validation and limits as
[local messaging](MESSAGING.md), [streams](STREAMS.md),
[shadow policies](SHADOWS.md) and [IPC](COMPONENT_IPC.md). Optional fields must
be omitted, rather than `null`. Unknown fields and unsupported versions fail
closed. Shadows still require a device-local `[shadows]` thing name and any
cloud-sync provisioning; this section grants shadow access.

## Device-local identity and precedence

Provision the IPC socket, administrator UIDs and one distinct UID per component
on the device. Deployments cannot assign UIDs or administrator status:

```toml
[ipc]
socket = "/run/suifleet/ipc.sock"
administrators = [1200]
[ipc.components.sensor]
uid = 1201
```

A UID-only component entry receives its fleet grants. If that entry contains
any operations, topic lists, policies or stream globs, it is an explicit local
override: the entire local ACL wins. Grants are never unioned with a restrictive
local ACL. Likewise, a local `[pubsub]` or `[streams]` section overrides the
whole corresponding fleet section, including its limits, mappings, definitions
and destinations. Local operator overrides remain independent of deployment
revocation. Two deployment roots cannot own the same grants or global service
section: conflicting ownership is rejected rather than using arrival order.

Fleet credential paths are denied by default. The operator must authorize each
exact absolute path, credential kind and destination in device-local
`config.toml` (these entries cannot be supplied by a recipe):

```toml
[[service_credentials]]
kind = "stream_bearer"
path = "/etc/suifleet/export.token"
destination = "https://ingest.example/v1/streams"

[[service_credentials]]
kind = "mqtt_password"
path = "/etc/suifleet/mqtt-password"
destination = "127.0.0.1:1883"
```

Destinations are matched byte-for-byte against the export URL or broker string;
there are no globs, redirects or cross-kind reuse. Only intentionally provisioned
service credentials belong in this allowlist, never device keys, signer PINs,
configuration or component overlays. Files must be regular, singly linked,
owned by root or the daemon UID and private (`0600` or `0400`); paths must be
absolute, normalized and contain no symlinks. Keep their parent directories
operator-controlled so components cannot replace authorized files. Rejection
happens before lifecycle commands and also on startup/recovery, including
references in fleet sections masked by local overrides. Changing or removing a
local authorization requires a daemon restart; ensure retained rollback
configurations remain authorized. Device-local service sections retain their
existing operator-controlled path behavior.

For migration, provision UIDs, private credentials and their allowlist first,
deploy a `1.3` recipe, then remove the local service sections and ACL fields that should
be fleet-managed and restart the daemon. Keep UID entries and the socket. Keep
keys, journals and stream directories. Recipe bytes are public: reference only
explicitly authorized credential paths, never passwords or tokens in the recipe.

## Deployment transaction and recovery

Before lifecycle validation, install or run commands, the daemon validates the
section, plan membership, ACLs, bridge loop rules, aggregate stream reservations,
persistence compatibility and the existing live stream inventory. Preflight
never opens/truncates stream segments, runs retention or evicts records. A
normal deployment shrink that would discard retained data is rejected. During explicit application
recovery, restored size/retention bounds take effect only after the journal
commits the recovered generation. Enabling a previously disabled stream service checks metadata only and conservatively rejects a
persisted reservation shrink.

The old generation stays active while component installation runs. After all
installs succeed, bridge/export tasks are stopped and joined, and service
metadata is staged without retention. A durable activation intent records the
old stream metadata and any new, not-yet-exposed stream directories. The journal
atomically commits the service generation with the deployment's Applied outcome.
IPC uses the effective policy on every request; bridge limits, mappings, exports
and shadow publication bindings are updated live. A post-commit activation
failure stops the daemon for recovery; it does not relabel the committed
transaction Failed or replay installation.

Failed installations leave the prior generation. Failed service activation
restores it. A crash before the journal commit restores old metadata and removes
only the newly staged streams; after the commit, startup projects the new
generation. The supervisor's explicitly configured application rollback restores
the prior service generation too. On startup, completed supervisor recovery
receipts settle configuration before stream loading or bridge/export tasks;
they are not evidence that a component is currently running or the OS booted
healthily. An interrupted lifecycle command remains
indeterminate and is never replayed automatically.

Each root replaces its complete section. Dropping a grant revokes it; deploying
that root without `services` revokes all its fleet grants and service sections.
Removing an installed component revokes its fleet grants. Removing a stream
declaration stops its export and marks it read-only (`stream_describe.retired`),
retaining records without TTL retirement. Deletion is explicit. A rollback can
restore a smaller active budget while retaining streams introduced by the newer
generation. Retained reservations still count against all new allocations:
existing overage may neither increase its stream count nor its reserved bytes.
Re-declaring an existing retained stream reactivates it only when active bounds
fit. Delete retained streams or explicitly raise the budget to allocate new ones. Local sections can still keep services enabled.

As with existing component configuration, lifecycle commands must not wait for
owner-loop IPC while the deployment is applying. Service permissions become
available after successful installation. Asynchronous firmware-helper-only and
daemon self-update recipes cannot change component services; use an ordinary
component deployment. Platform reboot and firmware recovery remain platform-owned.

Before staging service metadata, the journal binding gains a required-reader
`/services-v1` suffix. It retains the original chain/package/device/signer
identity. This reader accepts legacy journals and preserves the suffix on
restart; older daemons refuse the binding instead of ignoring retirement and
mutating retained records. Do not strip the marker to force a downgrade: older
binaries require an explicit supported state migration.

## Inspection and validation

IPC `config` returns a `services` summary containing effective grants, pub/sub
limits and mapping rules, stream limits/definitions, and whether exports are
enabled. It omits broker addresses, destinations, credential references, UIDs
and credential contents. `<state_dir>/services-effective.json` is the private,
redacted projection; signed telemetry includes its SHA-256 in the optional
`service_configuration_sha256` field. The fleet indexer accepts this bounded
digest; older telemetry without it remains valid. Upgrade the indexer before
rolling out this daemon, because older indexers reject unknown snapshot fields.

`make test-services` builds the real daemon and uses a disposable network-none
root container to exercise real SO_PEERCRED identities, deployment, upgrade,
failed replacement, quota rejection, restart, interrupted install, revocation,
retained data and independently verified signed exports. The unsealed host
container must refuse unattended restart after SIGKILL; the harness checks that
gate, kills its known fixture groups, verifies they are gone and explicitly
clears its own instance record before testing inspected restart. It does not
claim sealed-cgroup crash containment. Rust tests additionally
exercise schema, precedence, conflicting ownership, revocation, bridge loops,
read-only preflight and both activation crash decisions. This establishes host
software behavior; no physical Pi, production endpoint or hardware result is
claimed.
