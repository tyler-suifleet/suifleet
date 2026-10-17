# Local pub/sub and the MQTT bridge (SF-032)

Components exchange messages through the daemon's IPC socket ([COMPONENT_IPC.md](COMPONENT_IPC.md)). With `[pubsub]`, subscriptions are durable: messages are stored on disk, survive daemon restarts and wait for an acknowledgement. An optional bridge connects this pub/sub to a standard MQTT broker that serves LAN clients. Messages never go on chain or to Walrus. The rules follow the SF-028 specification ([GREENGRASS_COMPATIBILITY.md](GREENGRASS_COMPATIBILITY.md#messaging-sf-032)).

## Configuration

```toml
[ipc.components.sensor]
uid = 1101
operations = ["publish"]
[[ipc.components.sensor.pubsub_policies]]
operations = ["aws.greengrass#PublishToTopic"]
resources = ["sensors/*"]

[ipc.components.controller]
uid = 1102
operations = ["subscribe", "receive", "ack", "unsubscribe"]
[[ipc.components.controller.pubsub_policies]]
operations = ["aws.greengrass#SubscribeToTopic"]
resources = ["lan/commands/#"]

[pubsub]
max_messages_per_subscription = 1000      # 1..=100000, QoS 1
max_bytes_per_subscription = 4194304      # 4 KiB..=1 GiB, QoS 1
qos0_messages_per_subscription = 100      # 1..=10000; oldest dropped beyond

[pubsub.bridge]
broker = "127.0.0.1:1883"                 # host:port of the local broker
client_id = "suifleet-bridge"             # 1-23 of [A-Za-z0-9_-]
# username = "suifleet"
# password_file = "/etc/suifleet/mqtt-password"
keep_alive_seconds = 30                   # 5..=600

[[pubsub.bridge.mappings]]
topic = "sensors/#"                       # MQTT filter on the source side
source = "Pubsub"
target = "LocalMqtt"
target_topic_prefix = "device1/"          # optional; prepended on the target

[[pubsub.bridge.mappings]]
topic = "lan/commands/#"
source = "LocalMqtt"
target = "Pubsub"
```

`suifleet --check-config CONFIG_PATH` and daemon startup refuse invalid limits, filters, prefixes containing wildcards, a mapping whose source equals its target, more than 64 mappings, or a loop in which an outbound mapping's topics could match an inbound filter.

## Topics and authorization

- **Topics and filters follow MQTT 3.1.1/5 §4.7.** `+` matches one level, `#` the rest and only as the last level. Topic names contain no wildcards. Filters starting with a wildcard do not match `$` topics. Matching is case-sensitive; empty levels count.
- **Policies mirror `aws.greengrass.ipc.pubsub`.** Operations are `aws.greengrass#PublishToTopic`, `aws.greengrass#SubscribeToTopic` or `*`. A resource is a glob in which only `*` is special. `+` and `#` are literal, so `lan/commands/#` permits subscribing to exactly that filter, not to `lan/commands/x` or `lan/+/x`. Any matching policy grants; anything else is denied.
- **The principal is authenticated.** SO_PEERCRED identifies the calling UID, which must own the named component. A component cannot subscribe, receive or acknowledge as another.
- **Legacy SF-027 lists still apply.** `publish` passes on the legacy `publish` list or a `PublishToTopic` policy; `receive` on the legacy `subscribe` list or a `SubscribeToTopic` policy. `subscribe`, `unsubscribe` and `ack` need a `SubscribeToTopic` policy. `messaging` (counters) is administrator only.

## Operations

| Operation | Request | Result |
|---|---|---|
| `subscribe` | `topic` = filter, `payload` = `{"qos": 0 \| 1}` (default 1) | Durable subscription, stored on disk |
| `unsubscribe` | `topic` = filter | Removes it and its queue |
| `publish` | `topic` = name, JSON `payload` ≤ 4096 bytes | Stored for every matching durable subscription, then the volatile SF-027 inboxes. Returns the counts |
| `receive` | `topic` = a subscribed filter | The oldest message `{id, topic, sender, payload, published_ms}`, or null. QoS 1 messages stay at the head until acknowledged; QoS 0 are removed when read |
| `ack` | `topic` = filter, `payload` = message id | Removes an acknowledged QoS 1 message |
| `messaging` | administrator | Per-subscription `queued`, `bytes`, `dropped`, `refused`, and the bridge's state |

**Delivery.** QoS 1 is at least once and ordered per subscription: a message is redelivered until acknowledged, including after a daemon restart, so consumers must tolerate duplicates. Each message is written and synced before `publish` returns.

**Bounds, never silent loss.**
- A QoS 1 publication is all or nothing: if any matching QoS 1 queue is full (messages or bytes), nothing is stored, the publisher gets `backpressure` and each full subscription's `refused` counter increases.
- Byte limits count each message as it is stored on disk (the JSON envelope with id, topic, sender, payload and timestamp, including escaping).
- A storage failure on any target (for example a full disk) is also `backpressure`: the copies already written are removed and no subscriber receives the publication. Each copy is staged, synced and renamed into place, and the synced `<state_dir>/pubsub/seq` file then commits it. On start the daemon removes queue files above `seq`, so a crash part way through a fanout delivers to nobody either. If a failed publication's copies cannot be removed, publishing is refused until the daemon restarts.
- QoS 0 queues drop their oldest message and count it in `dropped`.

## The bridge

The bridge is an MQTT 3.1.1 client (MQTT 5 brokers accept it) with a persistent session (`clean session = 0`), run inside the daemon. A standard broker, for example Eclipse Mosquitto, serves LAN clients; the daemon does not embed one.

- **Pubsub → LocalMqtt.** Each outbound mapping is a durable QoS 1 subscription of principal `@bridge/<index>`, so its spool is the same bounded on-disk queue and the same limits apply. One message per mapping is in flight; it is acknowledged only after the broker's PUBACK and resent with DUP after a reconnect. Order per mapping is preserved. While the broker is down, publications accumulate until the queue is full; beyond that publishers get `backpressure` and `refused` grows.
- **LocalMqtt → Pubsub.** The bridge subscribes at QoS 1 and publishes each message into pub/sub as `@bridge/LocalMqtt`. It sends PUBACK only after the message is stored. If a subscriber's queue is full it disconnects without PUBACK, counts the refusal and reconnects later, so the broker keeps and redelivers the message.
- **Loops.** Messages sent by a bridge principal are not bridged out again, and configurations whose outbound topics could re-enter an inbound filter are refused.
- **Reconnect** backs off from 1 s to 60 s. Stats are written to `<state_dir>/pubsub/bridge.json` and served by `messaging`.
- **Transport.** Plain TCP to a broker on this device or a trusted network. TLS and authentication of LAN client devices are SF-035; cloud targets (AWS IoT Core or another remote broker) are a separately approved adapter (SF-061).

## SDK

```python
from suifleet_ipc import Client
controller = Client('/run/suifleet/ipc.sock', component='controller')
controller.subscribe('lan/commands/#', qos=1)
message = controller.receive('lan/commands/#')
if message:
    handle(message['payload'])
    controller.ack('lan/commands/#', message['id'])
Client(component='sensor').publish('sensors/a/temp', {'t': 21})
```

## Evidence

| Command | Covers |
|---|---|
| `make edge-test` | All 53 `interfaces/compat/pubsub-cases.json` cases against the daemon's module. QoS 1 order, redelivery and durability across reopen. Full queues refusing whole publications, QoS 0 dropping oldest. Invalid input. IPC policies (wildcard grants literal, no cross-component access, fail closed without `[pubsub]`). MQTT 3.1.1 encoding vectors and decoding. Bridge validation and loop detection |
| `make test-messaging` | The real daemon as root with two real component UIDs, the chain simulator and a real Eclipse Mosquitto 2.0.20 broker; LAN clients are `mosquitto_pub`/`mosquitto_sub`. ACL refusals. LAN → component at QoS 1 with receive and ack. Component → LAN under a prefix. Broker stopped: publications spooled and delivered in order after reconnect. Spool exhausted: refusals counted, then after a daemon restart exactly the accepted publications arrive in order. Inbound backpressure: a full subscriber queue leaves LAN messages with the broker; all arrive after draining. Receipt: [sf032-messaging.json](validation/sf032-messaging.json) |

**Not yet covered:**
- TLS, client-device certificates and per-client MQTT authorization (SF-035);
- cloud bridge targets (SF-061);
- the AWS IPC wire protocol (`aws_api` remains undecided);
- disk-full behaviour of the host filesystem beyond the configured bounds;
- physical Pi operation (#32).
