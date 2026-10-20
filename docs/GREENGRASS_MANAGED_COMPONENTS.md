# AWS-managed component and cloud-service adapter coverage

The [SF-064 remaining functional roadmap](GREENGRASS_REMAINING_ROADMAP.md) defines versioned executable subcontracts and maps every selected remaining scope to published implementation tickets SF-069–SF-079. Per-scope runtime receipts now include the [SF-072 media adapter](KINESIS_VIDEO.md); remaining scopes without receipts stay pending. No local receipt establishes live AWS acceptance.

SF-038 maps every AWS-provided Greengrass component to one of three things: a SuiFleet native equivalent, a scoped adapter with its own implementation ticket, or a roadmap gap tracked by [SF-064](https://github.com/tyler-suifleet/suifleet/issues/109). For each adapter it records the platforms, AWS service and authentication dependencies, licensing constraints and executable acceptance. The catalog is a specification with per-adapter runtime receipts. SF-057 implements the scoped Linux credential adapter; SF-058 implements the [Kinesis/S3 companion](STREAM_EXPORTERS.md) with local SDK/emulator evidence. Other adapters remain pending. It records no permanent exclusion and makes no general parity claim. It extends the [SF-028 compatibility specification](GREENGRASS_COMPATIBILITY.md), whose levels and statuses apply here.

The machine-readable source of truth is [`interfaces/compat/managed-components.json`](../interfaces/compat/managed-components.json). The [compatibility matrix](../interfaces/compat/greengrass-compat.json) cross-references it, and `make test-compat` checks both.

AWS reference: the [AWS-provided components](https://docs.aws.amazon.com/greengrass/v2/developerguide/public-components.html) and [machine learning components](https://docs.aws.amazon.com/greengrass/v2/developerguide/machine-learning-components.html) tables, checked 2026-10-15. Those tables have 46 components (the main table's "Machine learning components" row only points to the second table). A reference version is the latest changelog entry on the component's own page, and is recorded only where that page was read. An adapter implementation ticket must re-check and pin the exact AWS version before implementation. Four SiteWise Edge identifiers were not confirmed on a fetched page; the catalog notes say so.

## Scope and evidence state

Codex published the scoped implementation tickets below. SF-038 links them; it did not create them.

| Ticket | Scope |
|---|---|
| [SF-057](https://github.com/tyler-suifleet/suifleet/issues/102) | `credential-provider` (token exchange) |
| [SF-058](https://github.com/tyler-suifleet/suifleet/issues/103) | `stream-export-aws` (Kinesis Data Streams and S3) |
| [SF-059](https://github.com/tyler-suifleet/suifleet/issues/104) | `cloudwatch-metrics-export` |
| [SF-060](https://github.com/tyler-suifleet/suifleet/issues/105) | `cloudwatch-logs-export` |
| [SF-061](https://github.com/tyler-suifleet/suifleet/issues/106) | `aws-iot-core-connection` (IoT Core bridge and shadow sync) |
| [SF-062](https://github.com/tyler-suifleet/suifleet/issues/107) | `device-defender-metrics` (device side) |
| [SF-063](https://github.com/tyler-suifleet/suifleet/issues/108) | `secure-tunneling` destination adapter |
| [SF-064](https://github.com/tyler-suifleet/suifleet/issues/109) | Remaining roadmap: Lambda, industrial/SiteWise/Kinesis Video, ML example equivalents and other platforms |

Three kinds of evidence stay separate:

- **Reference models.** `make test-compat` checks the fixtures against small models. Agreement proves only that the fixtures are consistent.
- **Adapter execution.** The credential adapter has a [local runtime receipt](validation/sf057-credentials.json) and [provisioning guide](AWS_CREDENTIALS.md); the [SF-058 stream exporter receipt](validation/sf058-stream-export.json) adds actual durable-store and AWS API emulator execution. Other adapters retain `runtime_execution: pending`. Only an implementation ticket may run the same fixtures and runtime tests against the real adapter and record a `runtime_evidence` receipt. A linked ticket is not runtime evidence.
- **Live AWS acceptance.** No AWS account, credential, purchase or live service mutation is authorized. Every live check stays pending until the user authorizes one. The Sui testnet grant does not cover AWS.

Until SF-037 (open) adopts the dependency classification, recipe dependencies on AWS-managed components stay rejected by the current translation subset.

| Disposition | Meaning |
|---|---|
| `native` | A SuiFleet subsystem owned by the listed tickets provides the function. No AWS interface is reproduced |
| `native-component` | No SuiFleet runtime change. The operator packages equivalent open software as an ordinary native component |
| `adapter` | Scoped adapter to the AWS service or its local interface, owned by its implementation ticket. Its specification is `complete` (pinned AWS version and executable reference suite) or `incomplete` (the ticket must finish it first) |
| `roadmap` | Not provided in the initial Linux prototype phase. SF-064 tracks it as missing functional work and links concrete SF-069–SF-079 implementation tickets through the remaining roadmap. This is not an approved permanent exclusion. The listed native equivalent is an interim workaround, not AWS API or recipe compatibility |
| `not-applicable` | No meaning for SuiFleet, including services AWS discontinued |

## Required families

| Family | AWS components (reference version) | Mapping | Tickets |
|---|---|---|---|
| Lambda | `LambdaManager` 2.3.9, `LambdaLauncher`, `LambdaRuntimes`, `LegacySubscriptionRouter`; Lambda-type `Modbus`, `KinesisFirehose` 2.1.10, `SNS` 2.1.10 | Roadmap gap. AWS's components are closed-source under the Greengrass Core Software License Agreement, so a clean-room Lambda invocation and subscription equivalent is SF-064 work. Meanwhile a function can be repackaged as a generic native component with an SF-014 `run` command; that is a workaround, not Lambda compatibility | SF-014, SF-037, SF-064 |
| ML inference | `variant.DLR` 1.6.16, `variant.TensorFlowLite` 2.5.15, four model stores; DLR/TFLite sample classifiers and detectors; `SageMakerEdgeManager` 1.3.6 | Runtimes and models become native components with authenticated Walrus artifacts. Example-equivalent inference components are SF-064 roadmap work. Edge Manager is not applicable: AWS discontinued it on 2024-04-26 | SF-011, SF-023, SF-064 |
| Token exchange | `TokenExchangeService` 2.0.3 | Adapter `credential-provider` | SF-057 |
| Metrics | `Cloudwatch` 4.0.0, `telemetry.NucleusEmitter` 1.0.14 | Native telemetry in SF-019. Adapter `cloudwatch-metrics-export` | SF-019, SF-032, SF-059 |
| Device Defender | `DeviceDefender` 4.0.0 | Native security metrics and alerts in SF-019/SF-020. Adapter `device-defender-metrics` uploads device-side metrics through `aws-iot-core-connection`; the AWS server-side Detect service is not replicated | SF-019, SF-020, SF-061, SF-062 |
| Remote support | `SecureTunneling` 2.0.1, `SystemsManagerAgent` | SSH daemon, firewall and service configuration stay platform-owned, in line with the [platform boundary](../AGENTS.md#platform-update-boundary--user-requirement). Adapter `secure-tunneling` is an explicitly configured, supervised destination proxy that coexists with that configuration. The open-source SSM agent can be packaged as a native component | SF-011, SF-022, SF-063 |
| Industrial | `Modbus` 2.1.11, SiteWise OPC UA collector, simulator, publisher and processor, `EdgeConnectorForKVS` 1.0.5 | Protocol collectors are native components over SF-032 pub/sub and SF-034 streams. SiteWise and Kinesis Video functions are SF-064 roadmap work | SF-011, SF-032, SF-034, SF-064 |
| Cloud export | `StreamManager` 2.3.1, `LogManager` 2.3.14, `SystemLogForwarder`, `KinesisFirehose`, `SNS` | Native streams (SF-034) and logs (SF-020). Adapters `stream-export-aws` and `cloudwatch-logs-export`. Firehose and SNS are SF-064 roadmap work | SF-020, SF-034, SF-058, SF-060, SF-064 |

## Component catalog

| Component | AWS type, OS | Disposition | Native equivalent or adapter | Tickets |
|---|---|---|---|---|
| `aws.greengrass.Nucleus` | nucleus; Linux, Windows | native | suifleet-edge daemon | SF-004, SF-014, SF-026 |
| `aws.greengrass.NucleusLite` | nucleus; Linux | not-applicable | Out of SF-028 scope | |
| `aws.greengrass.Cli` | plugin; Linux, Windows | native | Fleet CLI and local CLI over SF-027 | SF-024, SF-027 |
| `aws.greengrass.LocalDebugConsole` | plugin; Linux, Windows | roadmap | Interim: dApp and local CLI | SF-064 |
| `aws.greengrass.telemetry.NucleusEmitter` | plugin; Linux, Windows | native | SF-019 health reporting over local pub/sub | SF-019 |
| `aws.greengrass.Cloudwatch` | generic (2.x Lambda); Linux, Windows | adapter | `cloudwatch-metrics-export` | SF-019, SF-032, SF-059 |
| `aws.greengrass.DeviceDefender` | generic (2.x Lambda); Linux, Windows | adapter | `device-defender-metrics` | SF-019, SF-020, SF-062 |
| `aws.greengrass.TokenExchangeService` | generic; Linux, Windows | adapter | `credential-provider` | SF-027, SF-057 |
| `aws.greengrass.LambdaManager` | plugin; Linux | roadmap | Interim: generic native component | SF-064 |
| `aws.greengrass.LambdaLauncher` | generic; Linux | roadmap | Interim: SF-014 `run` | SF-064 |
| `aws.greengrass.LambdaRuntimes` | generic; Linux | roadmap | Interim: runtime in the image or a component | SF-064 |
| `aws.greengrass.LegacySubscriptionRouter` | generic; Linux | roadmap | Interim: SF-032 pub/sub | SF-064 |
| `aws.greengrass.SageMakerEdgeManager` | generic; Linux, Windows | not-applicable (discontinued) | Models as native artifacts | |
| `variant.DLR` | generic; Linux, Windows | native-component | Packaged runtime | SF-011 |
| `aws.greengrass.DLRImageClassification` | generic; Linux, Windows | roadmap | Interim: operator inference component | SF-064 |
| `aws.greengrass.DLRObjectDetection` | generic; Linux, Windows | roadmap | Interim: operator inference component | SF-064 |
| `variant.DLR.ImageClassification.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `variant.DLR.ObjectDetection.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `variant.TensorFlowLite` | generic; Linux, Windows | native-component | Packaged runtime | SF-011 |
| `aws.greengrass.TensorFlowLiteImageClassification` | generic; Linux, Windows | roadmap | Interim: operator inference component | SF-064 |
| `aws.greengrass.TensorFlowLiteObjectDetection` | generic; Linux, Windows | roadmap | Interim: operator inference component | SF-064 |
| `variant.TensorFlowLite.ImageClassification.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `variant.TensorFlowLite.ObjectDetection.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `aws.greengrass.SecureTunneling` | generic; Linux | adapter | `secure-tunneling` beside platform-owned SSH/firewall | SF-063 |
| `aws.greengrass.SystemsManagerAgent` | generic; Linux | native-component | Operator-packaged amazon-ssm-agent | SF-011, SF-022 |
| `aws.greengrass.Modbus` | lambda; Linux | native-component | Native Modbus component over pub/sub | SF-011, SF-032, SF-064 |
| `aws.iot.SiteWiseEdgeCollectorOpcua` | generic; Linux, Windows | roadmap | Interim: operator OPC UA collector into SF-034 | SF-064 |
| `aws.iot.SiteWiseEdgeOpcuaDataSourceSimulator` | generic; Linux, Windows | roadmap | None needed meanwhile | SF-064 |
| `aws.iot.SiteWiseEdgePublisher` | generic; Linux, Windows | roadmap | Interim: SF-034 export | SF-064 |
| `aws.iot.SiteWiseEdgeProcessor` | generic; Linux, Windows | roadmap | Interim: operator processing component | SF-064 |
| `aws.iot.EdgeConnectorForKVS` | generic; Linux | roadmap | Interim: operator video component with `credential-provider` | SF-064 |
| `aws.greengrass.StreamManager` | generic; Linux, Windows | native | SF-034 streams; `stream-export-aws` | SF-034 |
| `aws.greengrass.LogManager` | plugin; Linux, Windows | native | SF-020 logs; `cloudwatch-logs-export` | SF-020 |
| `aws.greengrass.SystemLogForwarder` | generic; Linux | native | SF-020 journal collection | SF-020 |
| `aws.greengrass.KinesisFirehose` | lambda; Linux | roadmap | Interim: SF-034 with `stream-export-aws` | SF-064 |
| `aws.greengrass.SNS` | lambda; Linux | roadmap | Interim: SF-020 alerts | SF-064 |
| `aws.greengrass.clientdevices.Auth` | plugin; Linux, Windows | native | SF-035 | SF-035 |
| `aws.greengrass.clientdevices.IPDetector` | plugin; Linux, Windows | native | SF-035 discovery | SF-035 |
| `aws.greengrass.clientdevices.mqtt.Bridge` | plugin; Linux, Windows | native | SF-032 bridge; IotCore target via `aws-iot-core-connection` | SF-032 |
| `aws.greengrass.clientdevices.mqtt.Moquette` | plugin; Linux, Windows | native | SF-032 broker | SF-032 |
| `aws.greengrass.clientdevices.mqtt.EMQX` | generic; Linux, Windows | native | SF-032 broker | SF-032 |
| `aws.greengrass.DiskSpooler` | plugin; Linux, Windows | native | SF-032 spool | SF-032 |
| `aws.greengrass.ShadowManager` | plugin; Linux, Windows | native | SF-033 shadows; sync via `aws-iot-core-connection` | SF-033 |
| `aws.greengrass.SecretManager` | plugin; Linux, Windows | native | SF-022 secrets (not AWS Secrets Manager) | SF-022 |
| `aws.greengrass.crypto.Pkcs11Provider` | plugin; Linux | native | SF-021 hardware signer | SF-021 |
| `aws.greengrass.DockerApplicationManager` | generic; Linux, Windows | native | SF-036 OCI components | SF-036 |

## Platforms and Windows

The initial prototype phase is the SF-028 Linux rows: `x86_64`, `aarch64` and `arm`. Every adapter targets exactly these. The checker rejects an adapter platform that has no native runtime or is outside the initial phase.

| Platform | Phase | Reason |
|---|---|---|
| Linux x86_64, aarch64, arm | Initial prototype | Development hosts, Yocto/Raspberry Pi and 32-bit ARM catalog selection |
| Windows | Roadmap gap (SF-074) | The daemon depends on systemd and cgroups (SF-014) and on Yocto packaging. Translation omits and reports Windows manifests meanwhile |
| macOS | Roadmap gap (SF-075) | No runtime. AWS lists only Linux and Windows for the catalog components |
| Linux x86 (32-bit) | Roadmap gap (SF-076) | Native daemon, release bundle and `x86` catalog selection with 32-bit software-runner evidence ([LINUX_X86.md](LINUX_X86.md)). Managed adapters and AWS recipe translation do not target it; no Yocto image |

These gaps are not approved exclusions; they stay tracked unless the user changes the full parity goal. Windows support would need its own supervisor, service packaging, filesystem permissions and the full test suite. It is not an adapter.

## Licensing constraints

- SuiFleet does not redistribute, wrap or execute closed-source AWS component artifacts. Roadmap work uses clean-room functional alternatives. A native wrapper around open software is not, by itself, AWS API or recipe compatibility. AWS releases several of them (for example CloudWatch metrics, Device Defender and Modbus-RTU) under the Greengrass Core Software License Agreement.
- Adapters are clean-room implementations of documented protocols: the container-credentials endpoint, AWS service APIs and MQTT. They may use the Apache-2.0 AWS SDKs. The tunneling adapter runs the Apache-2.0 AWS IoT Secure Tunneling Local Proxy.
- Before reusing an "open source" AWS component, its implementation ticket must verify that component's repository license.
- Operators who package third-party software (for example BSD-licensed pymodbus or amazon-ssm-agent) as native components own that software's license compliance.

## Adapters

All adapters run as supervised local services. Components reach them only through SF-027 grants, deny by default. Credentials and data never go on chain. Each `runtime_tests` list in the catalog is the required acceptance for the implementation ticket. Real AWS-account runs are listed under `deferred`; they are unrun and need a separate user authorization for an AWS account.

AWS credentials are a separate trust domain. The `credential-provider` and `aws-iot-core-connection` adapters use an explicitly configured, separately provisioned AWS X.509 certificate and key. The Sui device signer (SF-021, SF-053) and Seal identity create no AWS certificate. The AWS key may be held through SF-021 hardware signer support or delivered through SF-022 when protected distribution is selected. Implementation can proceed against a local mock CA and endpoints before any production credential distribution.

| Adapter | Ticket | Specification | Replaces | AWS services | Auth | Depends on |
|---|---|---|---|---|---|---|
| `credential-provider` | SF-057 | complete | TokenExchangeService 2.0.3 | AWS IoT credentials provider (role alias) or IAM Roles Anywhere; STS | Separate AWS X.509 identity; per-component bearer token | SF-021, SF-027, SF-053 |
| `stream-export-aws` | SF-058 | complete | StreamManager 2.3.1 export | Kinesis Data Streams, S3 | SF-057; `kinesis:PutRecords`; S3 PutObject/GetObject/AbortMultipartUpload + ListBucketMultipartUploads ([scope](STREAM_EXPORTERS.md)) | SF-034, SF-057 |
| `cloudwatch-metrics-export` | SF-059 | complete | Cloudwatch 4.0.0 | CloudWatch | credential-provider; `cloudwatch:PutMetricData` | SF-019, SF-032, SF-057 |
| `cloudwatch-logs-export` | SF-060 | complete | LogManager 2.3.14 upload | CloudWatch Logs | credential-provider; `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` | SF-020, SF-057 |
| `aws-iot-core-connection` | SF-061 | incomplete | Bridge IotCore target, shadow sync | AWS IoT Core | Separate AWS IoT thing certificate and policy | SF-021, SF-032, SF-033 |
| `device-defender-metrics` | SF-062 | incomplete | DeviceDefender 4.0.0 | Device Defender (device-side metrics), IoT Core | `aws-iot-core-connection` | SF-019, SF-061 |
| `secure-tunneling` | SF-063 | incomplete | SecureTunneling 2.0.1 | IoT secure tunneling, IoT Core | `aws-iot-core-connection`; ephemeral per-tunnel token | SF-014, SF-025, SF-027, SF-061 |

The `stream-export-aws` adapter leaves out three destinations:

- `IoTAnalyticsConfig`, because AWS IoT Analytics was discontinued on 2025-12-15;
- `IoTSiteWiseConfig`, which is SF-064 roadmap work;
- `HTTPConfig`, because generic HTTP export is native SF-034 scope.

An `incomplete` specification names its `specification_gap`. SF-061 must pin the Bridge and ShadowManager versions it matches and add a reference suite. SF-062 and SF-063 must add executable schema/topic and notification/destination suites.

The `secure-tunneling` adapter is explicitly enabled and starts the local proxy only for allow-listed local services. SSH daemon, firewall and service configuration stay with platform scripts and administrators. The adapter neither manages nor replaces them, runs no arbitrary remote commands and never triggers a platform reboot.

### Credential provider contract

The `credential-provider` adapter is fixed by `credential-provider-cases.json` (65 steps across 7 sequences):

- **Environment.** A component granted access through SF-027 is started with `AWS_CONTAINER_CREDENTIALS_FULL_URI=http://127.0.0.1:{port}/2016-11-01/credentialprovider/` and a fresh `AWS_CONTAINER_AUTHORIZATION_TOKEN`. A component without a grant gets neither variable.
- **Tokens.** Each token belongs to one component start. Stopping, restarting or revoking the component invalidates it.
- **Requests.** Only `GET` on that exact path is served. Anything else is `method-not-allowed` or `not-found`. A missing, unknown or revoked token is `unauthorized`, and none of these reach upstream.
- **Response.** The HTTP body holds exactly `AccessKeyId`, `SecretAccessKey`, `Token` and `Expiration` (ISO 8601 UTC).
- **Caching.** Cached credentials are served while more than 300 seconds remain before expiry. At or below 300 seconds the adapter refreshes upstream.
- **Upstream failures.** A failure starts a backoff: 120 s for a client error, 60 s for a server error and 300 s for an unknown error. These equal the TokenExchangeService `credentialRetryInSec` defaults. During the backoff, still-valid cached credentials are served. Expired ones give `unavailable` with `retry_after`.
- **Storage.** Credentials stay in memory. They never reach disk, chain, Walrus, journals or logs.

Grants, per-component tokens and the refresh margin are SuiFleet proposals. The AWS service lets every component on a device use the device role.

### CloudWatch metrics export contract

The `cloudwatch-metrics-export` adapter is pinned to `aws.greengrass.Cloudwatch` 4.0.0 and fixed by `cloudwatch-metrics-cases.json` (90 steps across 8 sequences):

- **Requests.** It accepts the 4.0.0 `cloudwatch/metric/put` message: a `request` holding exactly `namespace` and `metricData`. The namespace has 1–255 characters, does not start with `:` and does not start with `AWS/`. `metricData` holds `metricName`, a numeric `value` that is 0 or has magnitude 8.515920e-109 to 1.174271e+108, up to 10 `dimensions`, a non-negative `timestamp` and a 4.0.0 `unit` string (default `None` when omitted; any other JSON type, including `null`, is invalid). Booleans, NaN and infinities are rejected. An integer of any size is compared exactly against these bounds, so one too large for a float is rejected like any other out-of-range value. Every dimension needs a non-empty name and value, and `coreName` is reserved for the adapter. Anything else gets `{"status": "fail", "error": "InvalidInputException"}`.
- **Grants.** Only publishers granted through SF-027 may put metrics. Others get `Unauthorized`. This is a SuiFleet proposal.
- **Publishing.** Metrics go out when `PublishInterval` has elapsed (default 10 s, at most 900 s; 0 publishes on every tick), or as soon as one namespace holds 20 metrics. Each `PutMetricData` call carries at most 20 metrics of one namespace, and namespaces go out in first-arrival order.
- **Retention.** `MaxMetricsToRetain` defaults to 5000 and must be at least 2000. When the buffer is full, a new metric replaces the oldest metric in its own namespace. If its namespace has no older metric, the new metric is dropped. Every drop is counted.
- **Upstream results.** Throttling, server errors and connection errors keep the batch for the next publish. An invalid-request response discards the batch and counts it as `rejected`.
- **Restart.** The buffer survives an adapter restart, and the interval restarts from the restart time. This is a SuiFleet proposal; the AWS component loses its buffer.

### CloudWatch Logs export contract

The `cloudwatch-logs-export` adapter is pinned to `aws.greengrass.LogManager` 2.3.14 and fixed by `cloudwatch-logs-cases.json` (69 steps across 8 sequences):

- **Sources and levels.** Only configured sources are uploaded. Each source is a `UserComponent` or a `GreengrassSystemComponent`. `minimumLogLevel` defaults to `INFO` from `DEBUG`, `INFO`, `WARN`, `ERROR`. Lower levels are filtered out, and unknown levels are rejected.
- **Names.** Events go to log group `/aws/greengrass/{componentType}/{region}/{componentName}` and log stream `/{yyyy/MM/dd}/thing/{thingName}`. The date is the event's UTC day, and `:` in the thing name becomes `+`. An event time must be a finite number of seconds from 0 up to, but not including, 253402300800 (the start of year 10000), so that it names a UTC day; any other time is `invalid` and is not spooled.
- **Uploads.** An upload runs every `periodicUploadIntervalSec`, which defaults to 300 s. Each source's events are sent in chronological order; equal timestamps keep arrival order. A `PutLogEvents` batch holds at most 10,000 events and 1,048,576 bytes, counting 26 bytes per event, all from one UTC day. As in LogManager 2.2.2 and later, a line over 256 KiB (262,144 bytes counting the 26) is split into consecutive events with the same timestamp, cut only at UTF-8 character boundaries, so no event or batch exceeds a limit. Each `log` result reports how many `events` the line became; the fixture field `repeat` abbreviates a long line as `message` repeated that many times.
- **Checkpoint.** Each source's checkpoint advances only on a successful response. After a throttled, server, connection or lost-response result, that source sends nothing further in the cycle, and the next cycle resends the same events unchanged. Delivery is therefore at least once, never lossy. Other sources are not affected.
- **Redaction and spool.** These are SuiFleet proposals. Configured secrets are replaced with `[REDACTED]` before an event is spooled. The spool is durable across restarts and bounded in bytes. When it is full, the oldest events are dropped and counted. A line whose events together exceed the whole spool is dropped by itself with status `dropped`; it evicts nothing, and each of its events is counted.

## Recipe dependency classification (for SF-037)

`managed-dependency-cases.json` (47 cases) fixes what an AWS recipe's `ComponentDependencies` entry on a managed component will mean. It does not change the current translation subset, which still rejects every `aws.greengrass.*` dependency until the open SF-037 translation ticket adopts this classification.

| Catalog state | Outcome |
|---|---|
| `native` | `replace`: the dependency is dropped with guidance naming the owning tickets. The version range is validated but has no native meaning |
| `native-component` | `package-natively`, naming the tickets for the operator's replacement component |
| `adapter`, complete specification | `defer` until the adapter's dependencies and its implementation ticket land. The AWS range must be satisfied by the adapter's pinned AWS version; otherwise `unsupported-aws-version` |
| `adapter`, incomplete specification | `reject` with `adapter-specification-incomplete`, naming the implementation ticket |
| `roadmap` | `reject` with `roadmap-gap`, naming SF-064 |
| `not-applicable`, discontinued | `reject` with `not-applicable` or `discontinued-aws-service` |
| Unlisted `aws.greengrass.*`, `aws.iot.*` or `variant.*` | `reject` with `unknown-managed-component`. Names are case-sensitive |
| Any other name | `not-managed`: the SF-037 native dependency rules apply |

Ranges use the native comparator grammar. x-ranges, hyphen ranges, `||` and non-string requirements give `unsupported-version-range`. A missing requirement means `*`.

## Acceptance suite

```bash
make test-compat
```

The checker validates the catalog's structure and its cross-references to the backlog, the SF-028 platforms and the suites. It also evaluates the four SF-038 suites against reference models in `scripts/greengrass_managed.py`. As with SF-028, these models only make the fixtures checkable. Agreement with them proves nothing about an adapter. Adapters stay `runtime_execution: pending` until their implementation ticket executes the declared runtime checks and commits a receipt. SF-057, SF-058 and SF-059 now have scoped local runtime receipts; the other adapters remain pending. Reference models still establish no service/account acceptance.

| Suite | Cases | Runtime execution |
|---|---|---|
| `managed-dependency-cases.json` | 47 classifications | Pending SF-037 |
| `credential-provider-cases.json` | 65 steps across 7 sequences | Pending SF-057 |
| `cloudwatch-metrics-cases.json` | 90 steps across 8 sequences | Passed: local adapter/HTTP/SQLite ([receipt](validation/sf059-metrics.json)); AWS delivery pending |
| `cloudwatch-logs-cases.json` | 69 steps across 8 sequences | Pending SF-060 |

A `complete` adapter specification must pin an exact AWS version and name at least one executable reference suite; the checker enforces both. `stream-export-aws` uses the SF-028 `stream-cases.json` checkpoint suite.

## Remaining work

SF-038 does not create tickets ([AGENTS.md](../AGENTS.md#ticket-authority--user-requirement)). The adapter tickets SF-057 to SF-063 and the roadmap ticket SF-064 were published by Codex and are linked above. What remains is outside this specification:

1. SF-057 to SF-063 implement the adapters, complete the `incomplete` specifications and record runtime receipts.
2. SF-064 splits the remaining Lambda, industrial, ML and platform roadmap into concrete tickets before any of it is implemented.
3. The open SF-037 translation ticket adopts `managed-dependency-cases.json` for managed dependencies.
4. Live AWS acceptance for every adapter needs a separate user authorization for an AWS account. None exists, and none of those checks has run.

SF-059 runtime evidence: the opt-in Linux [CloudWatch metrics companion](CLOUDWATCH_METRICS.md)
now executes the 90 pinned metrics fixture steps against its SQLite queue and
HTTP-observed SigV4 transport, plus permission/restart/bounds cases. This is
separate from reference-only conformance; actual AWS delivery remains unrun and
unauthorized in #104, and physical integration remains #32.
