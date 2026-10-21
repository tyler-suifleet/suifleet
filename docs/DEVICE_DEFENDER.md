# Device Defender device metrics (SF-062)

The opt-in `device-defender-metrics` adapter is pinned to the device-side behavior of `aws.greengrass.DeviceDefender` **4.0.0** and the **1.0 JSON long-name metrics document**, checked 2026-10-21. It collects Linux metrics and sends them through the existing [AWS IoT connection](AWS_IOT.md). It implements no AWS server-side Detect engine, security profiles, alerts or AWS policy evaluator. AWS currently states that Detect is unavailable to new customers; operators must verify account eligibility before any separately authorized live acceptance.

References: [component version/configuration](https://docs.aws.amazon.com/greengrass/v2/developerguide/device-defender-component.html), [device metrics document](https://docs.aws.amazon.com/iot-device-defender/latest/devguide/detect-device-side-metrics.html), [service responses](https://aws.amazon.com/jp/blogs/news/detect-anomalies-connected-devices/), [malformed report troubleshooting](https://docs.aws.amazon.com/iot-device-defender/latest/devguide/device-defender-troubleshoot.html).

## Configuration and authority

Add to the operator's config alongside `[aws_iot]` and `[pubsub]`:

```toml
[device_defender]
sample_interval_seconds = 300  # 300..=86400; default 300
publish_retry_count = 5        # 0..=72 retries after the initial attempt
buffer_max = 12                # 1..=128 reports, oldest dropped on overflow
```

The thing name, credentials, endpoint and reconnect/rotation policy come exclusively from `[aws_iot]`. No second MQTT connection or Sui signing identity is used. This local configuration is outside the fleet recipe services schema: components cannot enable reporting, change destinations, inject metrics or change sampling. Both inward and outward IotCore bridge mappings that overlap Defender reserved topics are rejected, including wildcard/prefix mappings. Component local pub/sub messages cannot become service responses. As with SF-061, macOS refuses the connection; Windows does not implement this adapter.

The AWS thing policy must permit publishing only to `$aws/things/THING/defender/metrics/json`, and subscribing to and receiving its `/accepted` and `/rejected` topics. Existing `iot:Connect` permission is also required. The adapter uses two additional subscriptions, counted against the connection's limit of 50. Subscription refusals remain visible through administrator `messaging` status; a policy-denied publish may disconnect the connection. Neither condition proves successful export.

## Collection and limits

The sampler reads the daemon's network namespace via `/proc/net/dev`, `tcp`, `tcp6`, `udp` and `udp6`, plus the kernel boot ID. Reports contain TCP/UDP listening port sets and established TCP endpoints, including IPv6. These expose network metadata to AWS; enable only when that destination is authorized. Optional interface labels are omitted because a socket binding does not identify a unique interface. Port totals count distinct port numbers across bindings; UDP includes unconnected bound sockets.

Network byte/packet values are **deltas**, per interface, rather than lifetime counters. The first report and the first report after reboot omit network statistics while establishing a baseline. New interfaces establish their own baseline; counter resets use a nonnegative saturating delta. Failed samples do not advance the baseline, so the next successful delta spans the gap. Each input is capped at 4 MiB, interface count at 1024, socket rows at 4096 per protocol and serialized reports at 64 KiB. An oversize or unreadable input refuses the sample rather than reporting truncated data as a complete inventory. IPv6 files may be absent on a kernel without IPv6.

Sampling never catches up in a burst. Its last sample time survives daemon restart. A backwards wall-clock adjustment pauses sampling until the persisted interval elapses; report IDs remain strictly increasing even if the clock moves backwards. Missing procfs or permission errors are reported locally and retried at the configured sampling interval. This is the service's namespace view, not a guarantee that every host/container namespace is visible under a particular OS sandbox.

## Delivery and recovery

`STATE_DIR/device-defender/state.json` is atomically replaced and fsynced, including its directory. It stores the thing binding, report ID, baseline, bounded queue, attempt counts, next retry times, accepted/rejected/dropped counters and a bounded last error. New state files are owner-only. State reads are capped at 9 MiB; queued reports occupy at most 8 MiB plus metadata. Preserve this directory across restarts; changing thing identity requires explicitly archiving its old spool first.

Disconnected samples remain queued; disconnected time spends no publish attempts. The oldest report is sent at QoS 1 once connected. The persistent attempt allowance is consumed **before** network submission, including uncertain connection failures. Retries keep the same bytes/report ID, with exponential delays capped at 300 seconds; a crash can consume an attempt without a send, but cannot reset its budget. Reports that exhaust the configured retries are dropped and counted with delivery unknown. Overflow drops the oldest report and counts it. A process restart can replay an uncertain send, so transport delivery is at least once within those bounds, not exactly once.

MQTT PUBACK is only transport acknowledgement. A service response must match the thing, exact outcome topic, outcome status and `reportId` to remove a report as accepted or rejected. A correlated rejection is counted separately, removed, and its details redacted/bounded; invalid payloads are not retried. Malformed, wrong-thing, stale-ID and inconsistent responses do not acknowledge the report. Some AWS malformed-payload responses omit `reportId`; these are ambiguous, time out after 30 seconds and consume bounded retries, never falsely acknowledging a different report. Acceptance proves parsing by the service, not healthy behavior or Detect coverage.

The request router prunes timed-out callers and never sends a queued request whose caller already timed out. It is shared with shadow synchronization and survives bridge task reconfiguration. A disk failure stops export rather than submitting unjournaled work. Sampling/export failures are logged without dumping metric payloads or credentials; counters and last error remain in the local state file.

## Verification and unrun acceptance

`make edge-test` executes the [pinned schema/topic/response fixture](../interfaces/compat/device-defender-cases.json) against the actual sampler and request router, including reset/reboot, interval, bound, restart, disconnected and correlation cases. `make test-compat` checks fixture consistency only. `make test-device-defender` runs the actual daemon, real procfs and the shared MQTT/mutual-TLS/ALPN adapter in a network-none container against the AWS-shaped endpoint. Its [receipt](validation/sf062-device-defender.json) distinguishes local evidence from AWS. CI runs this harness after building the daemon and shim.

Live AWS thing policy, actual Device Defender service responses and eligible-account Detect integration are **unrun**: no AWS account/credentials/service mutations are authorized. This gap remains in [SF-062/#107](https://github.com/tyler-suifleet/suifleet/issues/107); Sui testnet permission does not authorize AWS. Physical target acceptance remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32); actual mainnet acceptance remains [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). No image build, target flashing, mainnet or AWS deployment is claimed.
