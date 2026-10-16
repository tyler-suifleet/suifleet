# AWS-managed component and cloud-service adapter coverage

SF-038 maps every AWS-provided Greengrass component to one of three things: a SuiFleet native equivalent, a candidate adapter, or a recommended exclusion. For each candidate adapter it records the platforms, AWS service and authentication dependencies, licensing constraints and executable acceptance. This is a specification. It implements no adapter, approves no exclusion, creates no ticket and makes no parity claim. It extends the [SF-028 compatibility specification](GREENGRASS_COMPATIBILITY.md), whose levels and statuses apply here.

The machine-readable source of truth is [`interfaces/compat/managed-components.json`](../interfaces/compat/managed-components.json). The [compatibility matrix](../interfaces/compat/greengrass-compat.json) cross-references it, and `make test-compat` checks both.

AWS reference: the [AWS-provided components](https://docs.aws.amazon.com/greengrass/v2/developerguide/public-components.html) and [machine learning components](https://docs.aws.amazon.com/greengrass/v2/developerguide/machine-learning-components.html) tables, checked 2026-10-15. Those tables have 46 components (the main table's "Machine learning components" row only points to the second table). A reference version is the latest changelog entry on the component's own page, and is recorded only where that page was read. An adapter ticket must re-check and pin the exact AWS version before implementation. Four SiteWise Edge identifiers were not confirmed on a fetched page; the catalog notes say so.

## Decision state

Every disposition below is a **recommendation**. The checker refuses `approval: approved` without an existing `decision_ref`, and none is recorded. Until the user or Codex approves:

- a recommended exclusion stays `undecided` in the SF-028 matrix and never becomes `excluded`;
- a candidate adapter has no implementation ticket (`implementation_ticket: null`) and only a `ticket_request` for Codex;
- recipe dependencies on AWS-managed components stay rejected by the SF-037 subset.

| Disposition | Meaning |
|---|---|
| `native` | A SuiFleet subsystem owned by the listed tickets provides the function. No AWS interface is reproduced |
| `native-component` | No SuiFleet runtime change. The operator packages equivalent open software as an ordinary native component |
| `adapter` | Candidate adapter to the AWS service or its local interface. `recommended` adapters are proposed for approval now; `deferred` ones need a stated precondition first |
| `exclude` | Recommended exclusion awaiting approval |
| `not-applicable` | No meaning for SuiFleet |

## Required families

| Family | AWS components (reference version) | Recommendation | Tickets |
|---|---|---|---|
| Lambda | `LambdaManager` 2.3.9, `LambdaLauncher`, `LambdaRuntimes`, `LegacySubscriptionRouter`; Lambda-type `Modbus`, `KinesisFirehose` 2.1.10, `SNS` 2.1.10 | Exclude Lambda execution. These are closed-source under the Greengrass Core Software License Agreement, and emulating Lambda containers, event sources and subscriptions is a separate product. Repackage functions as generic native components with an SF-014 `run` command | SF-014, SF-037 |
| ML inference | `variant.DLR` 1.6.16, `variant.TensorFlowLite` 2.5.15, four model stores; DLR/TFLite sample classifiers and detectors; `SageMakerEdgeManager` 1.3.6 | Runtimes and models become native components with authenticated Walrus artifacts. Exclude the AWS sample applications. Exclude Edge Manager, which AWS discontinued on 2024-04-26 | SF-011, SF-023 |
| Token exchange | `TokenExchangeService` 2.0.3 | **Recommended adapter** `credential-provider` | SF-021, SF-022, SF-027 |
| Metrics | `Cloudwatch` 4.0.0, `telemetry.NucleusEmitter` 1.0.14 | Native telemetry in SF-019. **Recommended adapter** `cloudwatch-metrics-export` | SF-019, SF-032 |
| Device Defender | `DeviceDefender` 4.0.0 | Native security metrics and alerts in SF-019/SF-020. AWS Detect upload is a **deferred adapter** behind `aws-iot-core-connection` | SF-019, SF-020 |
| Remote support | `SecureTunneling` 2.0.1, `SystemsManagerAgent` | Remote access is platform-owned, in line with the [platform boundary](../AGENTS.md#platform-update-boundary--user-requirement). AWS tunnels are a **deferred adapter**. The open-source SSM agent can be packaged as a native component | SF-011, SF-022 |
| Industrial | `Modbus` 2.1.11, SiteWise OPC UA collector, simulator, publisher and processor, `EdgeConnectorForKVS` 1.0.5 | Protocol collectors are native components over SF-032 pub/sub and SF-034 streams. Exclude the closed-source SiteWise and KVS components | SF-011, SF-032, SF-034 |
| Cloud export | `StreamManager` 2.3.1, `LogManager` 2.3.14, `SystemLogForwarder`, `KinesisFirehose`, `SNS` | Native streams (SF-034) and logs (SF-020). **Recommended adapters** `stream-export-aws` and `cloudwatch-logs-export`. Exclude the Lambda Firehose and SNS components | SF-020, SF-034 |

## Component catalog

| Component | AWS type, OS | Disposition | Native equivalent or adapter | Tickets |
|---|---|---|---|---|
| `aws.greengrass.Nucleus` | nucleus; Linux, Windows | native | suifleet-edge daemon | SF-004, SF-014, SF-026 |
| `aws.greengrass.NucleusLite` | nucleus; Linux | not-applicable | Out of SF-028 scope | |
| `aws.greengrass.Cli` | plugin; Linux, Windows | native | Fleet CLI and local CLI over SF-027 | SF-024, SF-027 |
| `aws.greengrass.LocalDebugConsole` | plugin; Linux, Windows | exclude | dApp and local CLI; avoids a device web console | |
| `aws.greengrass.telemetry.NucleusEmitter` | plugin; Linux, Windows | native | SF-019 health reporting over local pub/sub | SF-019 |
| `aws.greengrass.Cloudwatch` | generic (2.x Lambda); Linux, Windows | adapter | `cloudwatch-metrics-export` | SF-019, SF-032 |
| `aws.greengrass.DeviceDefender` | generic (2.x Lambda); Linux, Windows | adapter (deferred) | `device-defender-metrics` | SF-019, SF-020 |
| `aws.greengrass.TokenExchangeService` | generic; Linux, Windows | adapter | `credential-provider` | SF-022, SF-027 |
| `aws.greengrass.LambdaManager` | plugin; Linux | exclude | Generic native component | |
| `aws.greengrass.LambdaLauncher` | generic; Linux | exclude | SF-014 `run` | |
| `aws.greengrass.LambdaRuntimes` | generic; Linux | exclude | Runtime in the image or a component | |
| `aws.greengrass.LegacySubscriptionRouter` | generic; Linux | exclude | SF-032 pub/sub | |
| `aws.greengrass.SageMakerEdgeManager` | generic; Linux, Windows | exclude (discontinued) | Models as native artifacts | |
| `variant.DLR` | generic; Linux, Windows | native-component | Packaged runtime | SF-011 |
| `aws.greengrass.DLRImageClassification` | generic; Linux, Windows | exclude | Operator inference component | |
| `aws.greengrass.DLRObjectDetection` | generic; Linux, Windows | exclude | Operator inference component | |
| `variant.DLR.ImageClassification.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `variant.DLR.ObjectDetection.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `variant.TensorFlowLite` | generic; Linux, Windows | native-component | Packaged runtime | SF-011 |
| `aws.greengrass.TensorFlowLiteImageClassification` | generic; Linux, Windows | exclude | Operator inference component | |
| `aws.greengrass.TensorFlowLiteObjectDetection` | generic; Linux, Windows | exclude | Operator inference component | |
| `variant.TensorFlowLite.ImageClassification.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `variant.TensorFlowLite.ObjectDetection.ModelStore` | generic; Linux, Windows | native-component | Authenticated model artifact | SF-011, SF-023 |
| `aws.greengrass.SecureTunneling` | generic; Linux | adapter (deferred) | `secure-tunneling`; platform-owned remote access | |
| `aws.greengrass.SystemsManagerAgent` | generic; Linux | native-component | Operator-packaged amazon-ssm-agent | SF-011, SF-022 |
| `aws.greengrass.Modbus` | lambda; Linux | native-component | Native Modbus component over pub/sub | SF-011, SF-032 |
| `aws.iot.SiteWiseEdgeCollectorOpcua` | generic; Linux, Windows | exclude | Operator OPC UA collector into SF-034 | |
| `aws.iot.SiteWiseEdgeOpcuaDataSourceSimulator` | generic; Linux, Windows | exclude | None needed | |
| `aws.iot.SiteWiseEdgePublisher` | generic; Linux, Windows | exclude | SF-034 export | |
| `aws.iot.SiteWiseEdgeProcessor` | generic; Linux, Windows | exclude | Operator processing component | |
| `aws.iot.EdgeConnectorForKVS` | generic; Linux | exclude | Operator video component with `credential-provider` | |
| `aws.greengrass.StreamManager` | generic; Linux, Windows | native | SF-034 streams; `stream-export-aws` | SF-034 |
| `aws.greengrass.LogManager` | plugin; Linux, Windows | native | SF-020 logs; `cloudwatch-logs-export` | SF-020 |
| `aws.greengrass.SystemLogForwarder` | generic; Linux | native | SF-020 journal collection | SF-020 |
| `aws.greengrass.KinesisFirehose` | lambda; Linux | exclude | SF-034 with `stream-export-aws` | |
| `aws.greengrass.SNS` | lambda; Linux | exclude | SF-020 alerts | |
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

SuiFleet's native platforms are the SF-028 Linux rows: `x86_64`, `aarch64` and `arm`. Every candidate adapter targets exactly these. The checker rejects an adapter platform without a native runtime.

| Platform | Recommendation | Reason |
|---|---|---|
| Windows | Exclude, pending approval | The daemon depends on systemd and cgroups (SF-014) and on Yocto packaging, and there is no user requirement for Windows. Translation already omits and reports Windows manifests |
| macOS | Exclude, pending approval | No runtime or user requirement. AWS lists only Linux and Windows for the catalog components |
| Linux x86 (32-bit) | Exclude, pending approval | No Yocto or test target |

A future Windows requirement would need its own ticket covering a supervisor, service packaging, filesystem permissions and the full test suite. It is not an adapter.

## Licensing constraints

- SuiFleet does not redistribute, wrap or execute closed-source AWS component artifacts. AWS releases several of them (for example CloudWatch metrics, Device Defender and Modbus-RTU) under the Greengrass Core Software License Agreement.
- Adapters are clean-room implementations of documented protocols: the container-credentials endpoint, AWS service APIs and MQTT. They may use the Apache-2.0 AWS SDKs. The deferred tunneling adapter would run the Apache-2.0 AWS IoT Secure Tunneling Local Proxy.
- Before reusing an "open source" AWS component, its adapter ticket must verify that component's repository license.
- Operators who package third-party software (for example BSD-licensed pymodbus or amazon-ssm-agent) as native components own that software's license compliance.

## Candidate adapters

All adapters run as supervised local services. Components reach them only through SF-027 grants, deny by default. Credentials and data never go on chain. Each `runtime_tests` list in the catalog is the required acceptance. Real AWS-account runs are listed under `deferred`, because they need a separate user authorization for an AWS account. That authorization is not covered by the Sui testnet grant.

| Adapter | Selection | Replaces | AWS services | Auth | Depends on |
|---|---|---|---|---|---|
| `credential-provider` | recommended | TokenExchangeService 2.0.3 | AWS IoT credentials provider (role alias) or IAM Roles Anywhere; STS | Device X.509 key from SF-021 or SF-022; per-component bearer token | SF-021, SF-022, SF-027 |
| `stream-export-aws` | recommended | StreamManager 2.3.1 export | Kinesis Data Streams, S3 | credential-provider; `kinesis:PutRecords`, `kinesis:ListShards`, `s3:PutObject` | SF-034 |
| `cloudwatch-metrics-export` | recommended | Cloudwatch 4.0.0 | CloudWatch | credential-provider; `cloudwatch:PutMetricData` | SF-019, SF-032 |
| `cloudwatch-logs-export` | recommended | LogManager 2.3.14 upload | CloudWatch Logs | credential-provider; `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` | SF-020 |
| `aws-iot-core-connection` | deferred | Bridge IotCore target, shadow sync | AWS IoT Core | Thing certificate and policy | SF-021, SF-022, SF-032 |
| `device-defender-metrics` | deferred | DeviceDefender 4.0.0 | Device Defender Detect, IoT Core | `aws-iot-core-connection` | SF-019, SF-020 |
| `secure-tunneling` | deferred | SecureTunneling 2.0.1 | IoT secure tunneling, IoT Core | `aws-iot-core-connection`; per-tunnel access token | SF-025 |

The `stream-export-aws` adapter excludes three destinations:

- `IoTAnalyticsConfig`, because AWS IoT Analytics was discontinued on 2025-12-15;
- `IoTSiteWiseConfig`, which is tied to the excluded SiteWise components;
- `HTTPConfig`, because generic HTTP export is native SF-034 scope.

The deferred adapters wait on a precondition: a user need for an AWS IoT Core thing identity alongside SuiFleet's chain identity. For tunneling, remote access must also be moved out of platform ownership.

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

The `cloudwatch-metrics-export` adapter is pinned to `aws.greengrass.Cloudwatch` 4.0.0 and fixed by `cloudwatch-metrics-cases.json` (66 steps across 6 sequences):

- **Requests.** It accepts the 4.0.0 `cloudwatch/metric/put` message: a `request` holding exactly `namespace` and `metricData`. The namespace has 1–255 characters, does not start with `:` and does not start with `AWS/`. `metricData` holds `metricName`, a numeric `value` that is 0 or has magnitude 8.515920e-109 to 1.174271e+108, up to 10 `dimensions`, a non-negative `timestamp` and a 4.0.0 `unit` (default `None`). Booleans, NaN and infinities are rejected. Every dimension needs a non-empty name and value, and `coreName` is reserved for the adapter. Anything else gets `{"status": "fail", "error": "InvalidInputException"}`.
- **Grants.** Only publishers granted through SF-027 may put metrics. Others get `Unauthorized`. This is a SuiFleet proposal.
- **Publishing.** Metrics go out when `PublishInterval` has elapsed (default 10 s, at most 900 s; 0 publishes on every tick), or as soon as one namespace holds 20 metrics. Each `PutMetricData` call carries at most 20 metrics of one namespace, and namespaces go out in first-arrival order.
- **Retention.** `MaxMetricsToRetain` defaults to 5000 and must be at least 2000. When the buffer is full, a new metric replaces the oldest metric in its own namespace. If its namespace has no older metric, the new metric is dropped. Every drop is counted.
- **Upstream results.** Throttling, server errors and connection errors keep the batch for the next publish. An invalid-request response discards the batch and counts it as `rejected`.
- **Restart.** The buffer survives an adapter restart, and the interval restarts from the restart time. This is a SuiFleet proposal; the AWS component loses its buffer.

### CloudWatch Logs export contract

The `cloudwatch-logs-export` adapter is pinned to `aws.greengrass.LogManager` 2.3.14 and fixed by `cloudwatch-logs-cases.json` (48 steps across 5 sequences):

- **Sources and levels.** Only configured sources are uploaded. Each source is a `UserComponent` or a `GreengrassSystemComponent`. `minimumLogLevel` defaults to `INFO` from `DEBUG`, `INFO`, `WARN`, `ERROR`. Lower levels are filtered out, and unknown levels are rejected.
- **Names.** Events go to log group `/aws/greengrass/{componentType}/{region}/{componentName}` and log stream `/{yyyy/MM/dd}/thing/{thingName}`. The date is the event's UTC day, and `:` in the thing name becomes `+`.
- **Uploads.** An upload runs every `periodicUploadIntervalSec`, which defaults to 300 s. Each source's events are sent in chronological order; equal timestamps keep arrival order. A `PutLogEvents` batch holds at most 10,000 events and 1,048,576 bytes, counting 26 bytes per event, all from one UTC day.
- **Checkpoint.** Each source's checkpoint advances only on a successful response. After a throttled, server, connection or lost-response result, that source sends nothing further in the cycle, and the next cycle resends the same events unchanged. Delivery is therefore at least once, never lossy. Other sources are not affected.
- **Redaction and spool.** These are SuiFleet proposals. Configured secrets are replaced with `[REDACTED]` before an event is spooled. The spool is durable across restarts and bounded in bytes. When it is full, the oldest events are dropped and counted, and an event larger than the whole spool drops itself.

## Recipe dependency classification (for SF-037)

`managed-dependency-cases.json` (47 cases) fixes what an AWS recipe's `ComponentDependencies` entry on a managed component will mean. It does not change the merged SF-037 subset, which still rejects every `aws.greengrass.*` dependency until SF-037 re-specifies it.

| Catalog state | Outcome |
|---|---|
| `native` | `replace`: the dependency is dropped with guidance naming the owning tickets. The version range is validated but has no native meaning |
| `native-component` | `package-natively`, naming the tickets for the operator's replacement component |
| `adapter`, recommended | `defer` until the adapter's dependencies and its implementation ticket land. The AWS range must be satisfied by the adapter's pinned AWS version; otherwise `unsupported-aws-version` |
| `adapter`, deferred | `reject` with `adapter-not-selected` |
| `exclude`, `not-applicable`, discontinued | `reject` with `excluded-component`, `not-applicable` or `discontinued-aws-service` |
| Unlisted `aws.greengrass.*`, `aws.iot.*` or `variant.*` | `reject` with `unknown-managed-component`. Names are case-sensitive |
| Any other name | `not-managed`: the SF-037 native dependency rules apply |

Ranges use the native comparator grammar. x-ranges, hyphen ranges, `||` and non-string requirements give `unsupported-version-range`. A missing requirement means `*`.

## Acceptance suite

```bash
make test-compat
```

The checker validates the catalog's structure and its cross-references to the backlog, the SF-028 platforms and the suites. It also evaluates the four SF-038 suites against reference models in `scripts/greengrass_managed.py`. As with SF-028, these models only make the fixtures checkable. Agreement with them proves nothing about an adapter. All four suites stay `runtime_execution: pending` until the owning implementation runs the same fixtures and commits a receipt.

| Suite | Cases | Runtime execution |
|---|---|---|
| `managed-dependency-cases.json` | 47 classifications | Pending SF-037 |
| `credential-provider-cases.json` | 65 steps across 7 sequences | Pending SF-022, SF-027 and an approved adapter ticket |
| `cloudwatch-metrics-cases.json` | 66 steps across 6 sequences | Pending SF-019, SF-032 and an approved adapter ticket |
| `cloudwatch-logs-cases.json` | 48 steps across 5 sequences | Pending SF-020 and an approved adapter ticket |

Each recommended adapter must pin an exact AWS version and name at least one executable reference suite; the checker enforces both. `stream-export-aws` uses the SF-028 `stream-cases.json` checkpoint suite. The deferred adapters still have unpinned versions and prose-only tests, and they must be specified fully before selection.

## Requests for Codex

SF-038 must not create tickets ([AGENTS.md](../AGENTS.md#ticket-authority--user-requirement)). Its third acceptance item, scoped implementation tickets for approved adapters, therefore stays open until Codex acts on these requests:

1. Approve or amend the recommended exclusions, including Windows, macOS and Linux x86, and record each decision in `decision_ref`.
2. If the four recommended adapters are approved, create their implementation tickets from each `ticket_request` and set `implementation_ticket`: `credential-provider`, `stream-export-aws`, `cloudwatch-metrics-export` and `cloudwatch-logs-export`.
3. Decide whether an AWS IoT Core thing identity is wanted. That decision unblocks the deferred `aws-iot-core-connection`, `device-defender-metrics` and `secure-tunneling` adapters.
4. Decide whether a real AWS account will be authorized for the deferred live checks. No such grant exists today.
5. For SF-037, adopt `managed-dependency-cases.json` as the migration guidance for managed dependencies.
