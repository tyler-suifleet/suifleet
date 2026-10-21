# SNS publication and event routing (SF-078)

[`scripts/sns_publish.py`](../scripts/sns_publish.py) is an opt-in,
platform-managed Python 3.10+ Linux companion. It consumes SNS publish requests
from a durable QoS 1 [local pub/sub](MESSAGING.md) subscription, enforces
per-component topic grants and calls SNS `Publish` with scoped SF-057
credentials. It is a clean-room functional alternative, not execution of AWS's
Lambda component or AWS recipe compatibility. No AWS component source, binaries
or license-restricted implementation was used. SNS fan-out, subscriptions,
retention and delivery to endpoints remain SNS service behaviour; nothing here
replicates them on the device.

The versioned contract and upstream references reviewed before implementation are
in [`sns-v1.json`](../interfaces/compat/sns-v1.json):
[Greengrass SNS 2.1.10](https://docs.aws.amazon.com/greengrass/v2/developerguide/sns-component.html),
[SNS API 2010-03-31 Publish](https://docs.aws.amazon.com/sns/latest/api/API_Publish.html)
and [SNS message attributes](https://docs.aws.amazon.com/sns/latest/dg/SNSMessageAttributes.html).
The pinned `botocore` 1.43.114 service model supplies SigV4, Query-protocol
serialization and refreshable container credentials; install the reviewed hashed
dependency set:

```sh
python3 -m pip install --require-hashes -r sdk/credentials/requirements.txt
```

## Operator configuration

Install code and dependencies outside writable component workspaces, with
`sdk/python/suifleet_ipc.py` beside the publisher. Provision a separate
platform-managed service UID as described in [AWS_CREDENTIALS.md](AWS_CREDENTIALS.md)
and a dedicated SF-057 provider whose IAM policy allows only `sns:Publish` on the
granted topic ARNs. The publisher loads only the container provider; ambient
static/profile credentials are not a fallback. Restart the credential wrapper
before its per-process bearer token expires. No credential provisioning, topic or
subscription creation is automatic.

Native IPC policy for the daemon (`[pubsub]` must be enabled):

```toml
[ipc.components.sns-publisher]
uid = 1201
operations = ["subscribe", "receive", "ack", "publish"]
[[ipc.components.sns-publisher.pubsub_policies]]
operations = ["aws.greengrass#SubscribeToTopic"]
resources = ["sns/message"]
[[ipc.components.sns-publisher.pubsub_policies]]
operations = ["aws.greengrass#PublishToTopic"]
resources = ["sns/message/status"]

[ipc.components.sensor]
uid = 1101
operations = ["publish"]
[[ipc.components.sensor.pubsub_policies]]
operations = ["aws.greengrass#PublishToTopic"]
resources = ["sns/message"]
```

Do not give the publisher a legacy `subscribe` list for the request topic.
Publisher configuration, one immutable mapping per private state directory:

```json
{
  "version": 1,
  "socket": "/run/suifleet/ipc.sock",
  "component": "sns-publisher",
  "region": "us-east-1",
  "request_topic": "sns/message",
  "status_topic": "sns/message/status",
  "default_topic_arn": "arn:aws:sns:us-east-1:123456789012:alerts",
  "grants": {
    "sensor": ["arn:aws:sns:us-east-1:123456789012:alerts"],
    "controller": ["arn:aws:sns:us-east-1:123456789012:ops.fifo"]
  },
  "max_attempts": 10,
  "max_publishes_per_second": 10,
  "state_dir": "/var/lib/suifleet-sns/alerts"
}
```

Run under the credential wrapper, in the foreground, as the dedicated UID:

```sh
python3 /usr/libexec/suifleet/suifleet_aws.py \
  --socket /run/suifleet-aws/credentials.sock --component sns-publisher -- \
  python3 /usr/libexec/suifleet/sns_publish.py --config /etc/suifleet/sns.json
```

All fields are required; `status_topic` and `default_topic_arn` may be `null`.
`request_topic` is an MQTT filter; `status_topic` is a topic name that the filter
must not match (no routing loop). Topic ARNs must be standard `aws` partition
SNS topics in `region`, because `Publish` only reaches same-Region topics.
`max_attempts` is 1..100 and `max_publishes_per_second` 0.01..1000.

The configuration and its ancestors must be operator-controlled and read-only to
components. The state directory must be publisher-owned, mode 0700, on a local
filesystem supporting flock, atomic rename and fsync, with trusted ancestors.
Configure the platform unit with `UMask=0077`, `NoNewPrivileges=yes`, core dumps
disabled and appropriate filesystem and network restrictions; do not mount AWS
profiles or expose SDK debug logs. One process holds an exclusive flock; a second
publisher using the same state is rejected. The configuration is bound to the
ledger, so a changed mapping or grant needs a new state directory after the
current queue drains. The durable subscription is created on first start;
requests published before then are not stored.

## Version 1 request and status

A request is the pub/sub payload; the Greengrass envelope plus explicit
attribute and FIFO fields:

```json
{"version": 1, "id": "request123", "request": {
  "message": "Door opened",
  "subject": "Alarm",
  "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:alerts",
  "message_attributes": {
    "zone": {"data_type": "String", "string_value": "north"},
    "level": {"data_type": "Number", "string_value": "3"},
    "tags": {"data_type": "String.Array", "string_value": "[\"a\",\"b\"]"},
    "frame": {"data_type": "Binary", "binary_value": "AAE="}
  }
}}
```

- `version` is optional and must be `1`; `id` (1..256 characters) is required and
  copied to the status. Unknown fields are rejected, not ignored.
- `message` is a nonempty string. `message_structure` may only be `json`; the
  message must then be a JSON object with a string `default` and no duplicate
  keys, and attributes are not allowed (SNS sends them only for string messages).
- `subject` is 1..99 characters with no control characters or leading space.
- `sns_topic_arn` overrides `default_topic_arn`; either must resolve to a topic.
- Up to 10 `message_attributes`. Names use `A-Z a-z 0-9 _ - .`, no leading,
  trailing or repeated period, no `AWS.`/`Amazon.` prefix. `String` and
  `String.Array` (a JSON array) values are nonempty strings without surrogates;
  `Number` is a decimal of at most five fraction digits within ±10^9; `Binary`
  is nonempty standard base64. Ten is the strictest documented consumer bound
  (SQS raw delivery); larger sets are rejected rather than silently dropped later.
- FIFO topics (`.fifo`) require `message_group_id`; `message_deduplication_id`
  is only accepted for FIFO topics. Both are 1..128 printable ASCII characters.
- The 256 KiB SNS size bound applies to body and attributes. In practice the
  SF-032 4,096-byte IPC payload limit is the effective bound; larger
  content belongs in an object store with a reference in the message.

The publisher reports each outcome on `status_topic`, shaped as Greengrass does:

```json
{"id": "request123", "response": {"status": "success", "sns_message_id": "f80a81bc-..."}}
{"id": "request123", "response": {"status": "fail", "error": "AccessDenied", "error_message": "sender is not granted this topic"}}
```

FIFO successes add `sns_sequence_number`. Local error codes are
`AccessDenied`, `InvalidInputException`, `UnsupportedVersion`, `RetryExhausted`
and `DeliveryUnknown`; permanent SNS refusals report the SNS code (for example
`InvalidParameter`, `NotFound`, `KMSDisabled`). `error_message` is fixed text;
payloads and SDK messages are never echoed. Statuses are at least once and may
repeat after a restart; correlate them by `id`. Grant status-topic subscriptions
only to components allowed to see every requester's outcomes. With
`status_topic: null` no status is published.

## Authorization and routing

The caller is the pub/sub `sender`, the component the daemon authenticated by
`SO_PEERCRED`; payload fields cannot name a caller. Grants map exact sender names
to exact topic ARNs: no wildcards, and the default topic is not implicitly
granted. The grant is checked before any other processing of the request and
before network I/O; a denied or invalid request is reported and acknowledged with
no SNS call. The SF-032 IPC policy decides who may publish requests at all;
grants decide where each sender's requests may go. LAN messages bridged in arrive
as `@bridge/LocalMqtt` and need an explicit grant; LAN clients are not
authenticated individually (SF-035).

## Delivery, backpressure and retry

Requests are processed one at a time in subscription order. The outcome is
written to a fsync'd, configuration-bound ledger before the status is published
and the request acknowledged. A redelivered request at or below the ledger's
completed id (after a lost ack, status backpressure, publisher crash or daemon
restart) is acknowledged and its status resent, never republished.

- **Backpressure.** The request subscription is the bounded SF-032 on-disk queue
  (`max_messages_per_subscription`, `max_bytes_per_subscription`). While SNS or
  the credentials are unavailable, requests accumulate; once full, publishers get
  an IPC refusal and the subscription's `refused` counter grows. Nothing is
  silently dropped. If the status topic's subscribers are full, the publisher holds
  the request (`status-pending`) without republishing until the status fits.
- **Rate.** A token bucket (burst = max(1, rate)) bounds SNS calls per second.
- **Retry.** Throttling (`Throttling`, `KMSThrottling`), SNS internal errors,
  timeouts, disconnects, IAM `AuthorizationError` and credential failures retain
  the request. SDK retries are disabled; the foreground loop backs off 1, 2, 4,
  ... 60 seconds. Each SNS call is counted durably before it is sent, so the bound
  survives restarts. After `max_attempts` calls the request is reported
  `RetryExhausted` (no attempt reached SNS ambiguously) or `DeliveryUnknown` (an
  attempt timed out or lost its response, so it may have been published) and
  acknowledged. Credential or connection failures before sending still count.
- **Duplicates.** A lost response followed by a retry can publish twice to a
  standard topic, which has no deduplication: delivery is at least once. For FIFO
  topics without an explicit `message_deduplication_id` the publisher sends a
  stable SHA-256 of the configuration binding and the pub/sub message id, so SNS
  drops a retry within its 5-minute deduplication interval; a retry after that
  interval can still duplicate. Pub/sub message ids belong to one daemon state
  directory; do not reset pub/sub state under an existing ledger.

Diagnostics on stderr are fixed codes: `throttled`, `retry-pending`, `ambiguous`,
`ambiguous response`, `status-pending`, `unsupported-message` (a legacy inbox
message without an id was consumed). Monitor the platform service, the
subscription's `queued`/`refused` counters (`messaging`) and the ledger; operator
intervention is needed for persistent IAM, credential, quota or topic failures.

## Evidence and limits

Prepare dependencies, build the debug daemon and gRPC simulator shim, then run:

```sh
python3 -m pip install --require-hashes --target .ci-cache/aws-python -r sdk/credentials/requirements.txt
# In the pinned development environment:
cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --examples
# From the host, using the pinned built image:
make test-sns SUIFLEET_DEV_IMAGE=suifleet-dev:ci
```

The network-none suite sends signed SDK Query requests to a local SNS/container
credential emulator that verifies SigV4 and models scoped IAM, missing topics and
FIFO deduplication: subject/attribute/json serialization, caller and topic denial
without requests, invalid inputs and versions, service throttling and the local
rate limit, lost responses duplicating on standard topics but not FIFO, durable
attempt bounds with `RetryExhausted`/`DeliveryUnknown`, permanent errors, lost
acks, status backpressure, an abrupt process exit after publish, credential
expiry and failed refresh, IAM narrower than the grant, ledger lock/binding/
corruption and failed fsync. The real-daemon run (`scripts/test_sns.py`) uses
distinct component UIDs: peer-authenticated routing to a signed request and a
watcher receiving statuses, IPC policy refusals, an ungranted caller denied,
queue-full backpressure during an outage followed by in-order delivery, and a
lost ack plus daemon SIGKILL whose redelivery is not republished. Ordinary CI
runs this suite. SF-064's pure `sns` reference vectors remain specification-only;
their catalog fields remain pending and are not runtime evidence.

Live AWS acceptance is **unrun and unauthorized**, tracked in
[SF-078/#132](https://github.com/tyler-suifleet/suifleet/issues/132): verify IAM
scope, real topic delivery, FIFO deduplication, throttling, refresh, cost and
subscriber behaviour only after explicit AWS authorization. Physical integration
is deferred to [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
No AWS account mutations, paid services, production credentials, physical image
or full-parity result is claimed. Sui contracts/transactions are unchanged;
automatic testnet CI remains the normal PR gate.
