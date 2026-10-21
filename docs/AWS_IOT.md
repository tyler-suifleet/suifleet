# AWS IoT Core connection (SF-061)

With `[aws_iot]`, the daemon keeps one MQTT 3.1.1 connection over mutual TLS to AWS IoT Core, as an AWS IoT thing. The connection carries:

- the pub/sub bridge's `IotCore` mappings ([MESSAGING.md](MESSAGING.md));
- AWS IoT Device Shadow synchronization ([SHADOWS.md](SHADOWS.md)).

It is the `aws-iot-core-connection` adapter of the managed-component catalog ([GREENGRASS_MANAGED_COMPONENTS.md](GREENGRASS_MANAGED_COMPONENTS.md)). It matches `aws.greengrass.clientdevices.mqtt.Bridge` 2.3.3 IotCore routes and `aws.greengrass.ShadowManager` 2.3.15 synchronization, with the differences listed below. The implementation is clean-room, from AWS documentation; it uses rustls for TLS and contains no AWS component code. It is supported on Linux first; macOS refuses `[aws_iot]` until it is validated there.

## Configuration

```toml
[aws_iot]
endpoint = "abc123-ats.iot.us-east-1.amazonaws.com"   # the account's data endpoint (host only)
port = 8883                                         # or 443, which uses ALPN x-amzn-mqtt-ca by default
# alpn = "x-amzn-mqtt-ca"                           # optional; set when a port other than 443 needs it
thing_name = "device1"
# client_id = "device1"                             # default: the thing name
ca_file = "/etc/suifleet/aws/AmazonRootCA1.pem"
certificate_file = "/etc/suifleet/aws/device.pem.crt"
private_key_file = "/etc/suifleet/aws/private.pem.key" # owner-only (0600)
keep_alive_seconds = 60                             # 30..=1200 (AWS IoT Core)
credential_check_seconds = 30                       # rotation check, 5..=3600
# reject_denied_publications = false                # see "A publication the policy denies"

[pubsub]                                            # required: the connection runs in the pub/sub bridge
[pubsub.bridge]                                     # broker is needed only by LocalMqtt mappings
[[pubsub.bridge.mappings]]
topic = "telemetry/#"
source = "Pubsub"
target = "IotCore"
target_topic_prefix = "dt/device1/"
[[pubsub.bridge.mappings]]
topic = "cmd/device1/#"
source = "IotCore"
target = "Pubsub"

[shadows]
thing_name = "device1"                              # must equal aws_iot.thing_name
[shadows.aws_iot]                                   # AWS IoT Device Shadow sync
shadows = ["", "config"]                            # "" is the classic shadow
interval_seconds = 300                              # cloud poll, 1..=3600; cloud changes are also notified
max_backoff_seconds = 300
desired_conflict = "cloud"
reported_conflict = "device"
```

`suifleet --check-config` and daemon startup refuse:

- an endpoint that is not a host name;
- a relative credential path;
- a keep-alive outside the AWS range;
- an invalid thing name or client id;
- a private key file readable by group or others;
- a certificate and key that do not belong together;
- `[aws_iot]` without `[pubsub]`;
- `[shadows.aws_iot]` without `[aws_iot]`, together with `[shadows.sync]`, or with a different thing name;
- more than the 50 subscriptions AWS IoT Core allows per connection. Each inbound IotCore mapping takes one, and each synchronized shadow takes three.

## Credentials and trust domains

The AWS IoT thing identity is separate from the Sui device identity:

- **Separate files.** The certificate, key and CA are provisioned separately (for example by AWS IoT fleet provisioning or by the operator). The Sui device key is never used for AWS, and configuring it as `private_key_file` (by path, symlink or hard link) is refused. The AWS key never signs Sui transactions.
- **Endpoint CA only.** The endpoint is verified against `ca_file` only, never the system roots.
- **What AWS authorizes.** The account's AWS IoT policy decides what the thing may do: connect with its client id, publish, subscribe and receive. SuiFleet does not replicate AWS policy evaluation. It reports what the endpoint refuses (below) and keeps components behind their own pub/sub grants ([COMPONENT_IPC.md](COMPONENT_IPC.md)).
- **No key material leaves the process.** Errors name a file and a problem, never key material. The certificate's SHA-256 is reported, and nothing else about the key.
- **PEM files only.** Keys held in PKCS#11 tokens (SF-021) are not yet supported for AWS.

**Rotation.** Every `credential_check_seconds`, the daemon re-reads the three files:

- **A complete new set.** If the files changed and the new certificate and key belong together, the connection closes and reopens with them. Status shows `rotations` and the new `certificate_sha256`.
- **An incomplete set.** For example, a new key with the old certificate is ignored while the current connection continues, and `credential_error` explains why.
- **While disconnected.** New files are tried at once, without waiting for the backoff.

Replace each file atomically (write, then rename). To rotate on AWS:

1. Register and attach the new certificate.
2. Install it on the device.
3. Confirm the new `certificate_sha256`.
4. Deactivate the old certificate.

## TLS and connection

- **TLS.** TLS 1.2 or 1.3 through rustls, with the thing certificate as client certificate. On port 443, ALPN `x-amzn-mqtt-ca` is offered by default. If a configured ALPN protocol is not accepted, the connection fails.
- **Session.** MQTT 3.1.1 with a persistent session (`clean session = 0`), so AWS keeps subscriptions and QoS 1 messages while the device is away. Keep-alive is checked against `keep_alive_seconds`.
- **Reconnect.** Backs off from 1 s to 60 s. The backoff resets after a connection that lasted a minute, or when new credentials are installed.
- **Subscriptions.** Sent in groups of at most 8, the AWS limit per SUBSCRIBE. A subscription AWS refuses (SUBACK 0x80) is logged and listed in `refused_subscriptions`; the rest of the connection continues.

## Bridge mappings to and from IotCore

Mappings connect `Pubsub` with `IotCore`, with the same durable spooling, limits and at-least-once delivery as LocalMqtt mappings ([MESSAGING.md](MESSAGING.md)):

- **Pubsub → IotCore.** Each mapping's durable spool keeps messages until AWS acknowledges them (PUBACK), across outages and restarts. One message is in flight at a time on the connection, so a refusal can be attributed. Throughput is about one message per round trip.
- **IotCore → Pubsub.** Messages are stored for every matching subscriber before PUBACK. If a subscriber queue is full, the message stays with AWS for redelivery.
- **Topic limits.** A topic beyond the AWS IoT Core limits is rejected and counted in `rejected`: 256 bytes and 7 slashes, not counting a Basic Ingest `$aws/rules/<rule>/` prefix. A mapping whose topics exceed them is refused at startup.
- **A publication the policy denies.** AWS IoT Core can disconnect a client whose publication its policy denies. A truncated connection looks the same, so a disconnect is not proof of a denial. If the connection closes within 5 s after a message was sent, three times in a row for the same message, the message is **held**:
  - It stays in the spool and is retried on each new connection, with the reconnect backoff. Everything behind it on the connection waits.
  - The mapping's `held` status names the topic and the reason, and a warning is logged.
  - A PUBACK delivers it, clears `held` and resets the count, for example once the policy is fixed. Transport errors, timeouts and closes before the message was resent do not count.
  - Only with `reject_denied_publications = true` is the message removed from the spool after the third such close, counted in `rejected` with `last_rejected`, so traffic behind it continues. That trades possible loss of a message that was not denied for availability; it is off by default.
- **Shadow topics.** `$aws/things/<thing>/shadow/...` belongs to shadow synchronization. A mapping that could publish there, subscribe there, or inject cloud messages into the local shadow notification topics is refused.
- **Loops.** Messages the bridge received from a broker are never sent back to that broker. They may continue to the other one: a LocalMqtt → Pubsub mapping and a Pubsub → IotCore mapping forward LAN messages to AWS. Outbound topics that could re-enter an inbound mapping of the same broker are refused.

`messaging` (administrator IPC) returns the bridge statistics, with an `iot_core` section:

- `endpoint`, `client_id`, `connected` and `connects`;
- `last_error`, `alpn` and `certificate_sha256`;
- `rotations`, `credential_error` and `refused_subscriptions`.

## AWS IoT Device Shadow synchronization

`[shadows.aws_iot]` runs the SF-033 reconciliation ([SHADOWS.md](SHADOWS.md#cloud-synchronization)) against the AWS Device Shadow service over the connection.

- **Requests.** Each request is a QoS 1 publication to `$aws/things/<thing>/shadow[/name/<n>]/{get,update,delete}` with a unique `clientToken`. The answer is the `accepted`/`rejected` message with that token, or a failure after 30 s. A request that timed out is forgotten, so answers that never arrive leave no state; at most 32 requests await an answer at once.
- **Read.** `get` gives the cloud version and its `desired`/`reported` sections. `delta` and `metadata` are ignored, and 404 means absent.
- **Write.**
  - The merged document is written as an `update` with the AWS merge patch from the cloud document: removed keys are `null`, objects are patched key by key, and other values are replaced whole.
  - The update carries `version` set to the version read, so a concurrent writer causes `409` and a fresh read and merge, and neither change is lost.
  - A deletion is a `delete`.
- **Conflicts.** The section owners settle conflicting keys: by default the cloud owns `desired` and the device owns `reported`.
- **Prompt sync.** Local changes are pushed at once. Cloud changes from other writers arrive as `update/documents` or `delete/accepted` notifications and start a cycle at once. `interval_seconds` is only a fallback poll.
- **Restarts.** The agreed base survives restarts and `kill -9`, so unchanged shadows are not rewritten.

**Not compare-and-swap.** AWS takes no version on `delete`, and a shadow that does not exist has no version to compare. A cloud edit made between the read and such a deletion is lost. Concurrent creations merge key by key on AWS. The window is one round trip.

AWS applies its own limits. For example, it refuses JSON nested deeper than 6 levels, while the local store allows 8 ([SHADOWS.md](SHADOWS.md#documents)). A document AWS refuses fails that shadow's synchronization with the AWS code and message in `shadows` status, and is retried with backoff.

## Differences from Greengrass

| Greengrass | SuiFleet |
|---|---|
| Bridge 2.3.3 `LocalMqtt` → `IotCore` mapping | Two mappings through `Pubsub` (refused directly) |
| Bridge `mqtt5RouteOptions`, MQTT 5 | MQTT 3.1.1 only |
| Nucleus IoT connection (configured with the core device) | `[aws_iot]` with explicit, separate credential files |
| ShadowManager `synchronize.direction` | Always both ways; conflicts settled by per-section owners |
| ShadowManager `strategy` realTime/periodic | Real time, plus cloud notifications and a fallback poll |
| ShadowManager `rateLimits`, `shadowDocumentSizeLimitBytes` | Local document limit 8 KiB; no rate limit |
| Other things' shadows (`shadowDocumentsMap`) | Only `aws_iot.thing_name` |

## Evidence

| Command | Covers |
|---|---|
| `make edge-test` | All 113 checks of [`interfaces/compat/aws-iot-cases.json`](../interfaces/compat/aws-iot-cases.json), which `scripts/greengrass_compat.py` also models: connection settings and ALPN defaults, AWS topic limits, IotCore mapping rules, shadow update patches, the synchronizer's requests and reading of AWS answers over the real request path, and the denied-publication rule through the real spool. Credential file checks without revealing keys. The Sui key refused as the AWS key by path, symlink and hard link. Configuration cross-checks (pub/sub, thing name, subscription quota, one cloud per shadow). |
| `make test-aws-iot` | The real daemon as root with a real component UID, the chain simulator and test certificates generated per run, in network-none containers. Two endpoints: Eclipse Mosquitto 2.0.20 with mutual TLS, CN identities, an ACL file and a CRL; and an AWS-shaped endpoint with per-thing policies and the Device Shadow service ([`scripts/aws_iot_harness.py`](../scripts/aws_iot_harness.py)). Scenarios: both bridge directions; broker outages with spooled delivery in order, including `kill -9`; a half-installed and a complete rotation (RSA to P-256), then the old certificate revoked by CRL; refused identities (CRL-revoked, unknown CA, deactivated, another thing's certificate) with recovery once valid credentials are installed; an untrusted server certificate; ALPN on 443 (a client without it is refused); a policy-denied subscription; a policy-denied publication held in the spool through repeated truncated disconnects and delivered once the policy allows it, and rejected with `reject_denied_publications`; shadow push, pull by notification, offline edits on both sides with conflicts by owner, a 409 version race, deletion both ways and no redundant writes after restart; no key material in logs or state. Receipt: [sf061-aws-iot.json](validation/sf061-aws-iot.json) |

**Not yet covered:**
- a real AWS IoT Core endpoint, account policy and Device Shadow service (needs separate user authorization);
- AWS keys in PKCS#11/TPM tokens;
- MQTT 5;
- macOS (refused at startup) and Windows;
- physical Pi operation (#32).
