# Remaining managed-component functional roadmap (SF-064)

This is a versioned implementation plan, not completed functional parity. The
[managed catalog](GREENGRASS_MANAGED_COMPONENTS.md) retains missing functions and
[remaining-roadmap.json](../interfaces/compat/remaining-roadmap.json) binds each
selected scope to its published issue, dependencies and acceptance. The seed
backlog and publication records contain the same scope. Existing native runtime,
IPC, stream, artifact and credential foundations are reused rather than duplicated.
SF-057–063 cover seven adapters; neither those tickets nor this plan establish
whole-catalog compatibility. Closing SF-064 completes the plan, not its children.

| Functional gap | Implementation | Dependencies | Required actual implementation evidence |
|---|---|---|---|
| Lambda invocation, runtimes and legacy subscriptions | [SF-069](https://github.com/tyler-suifleet/suifleet/issues/123) | SF-014/027/032/037 | Real on-demand and long-lived function processes; payload/error/timeout/concurrency, restart, isolation and subscription routing |
| Modbus, OPC UA and industrial simulator | [SF-070](https://github.com/tyler-suifleet/suifleet/issues/124) | SF-027/032/034 | Collector against deterministic protocol servers; typed timestamps/quality, security rejection, reconnect and backpressure |
| SiteWise processing and publishing | [SF-071](https://github.com/tyler-suifleet/suifleet/issues/125) | SF-070/034/057 | Measurement/property/alias mapping, transformations, request-checking emulator, partial failures and durable checkpoints |
| Kinesis Video ingestion/upload | [SF-072](https://github.com/tyler-suifleet/suifleet/issues/126) | SF-034/057 | Real media source, fragment/session/timestamp validation, bounded spool, expiry, reconnect and restart replay |
| ML runtimes, classifier/detector examples and model stores | [SF-073](https://github.com/tyler-suifleet/suifleet/issues/127) | SF-011/023 | Pinned reproducible runtime/model packages, licenses, preprocessing and output tolerances; actual inference and rejection fixtures |
| Windows runtime | [SF-074](https://github.com/tyler-suifleet/suifleet/issues/128) | SF-014/025/027 | Windows service runner, process containment, filesystem/secret ACLs, authenticated IPC and interrupted lifecycle |
| macOS runtime | [SF-075](https://github.com/tyler-suifleet/suifleet/issues/129) | SF-014/025/027 | macOS service runner, process containment, private identity, authenticated IPC and interrupted lifecycle |
| Linux x86 32-bit packaging/runtime | [SF-076](https://github.com/tyler-suifleet/suifleet/issues/130) | SF-023/025 | Declared ABI/toolchain, real 32-bit binary/package, emulator/runner lifecycle/IPC/identity/OTA acceptance |
| Firehose export | [SF-077](https://github.com/tyler-suifleet/suifleet/issues/131) | SF-034/057 | Actual batch requests, service limits, per-record failure, credentials, restart and checkpoint tests |
| SNS publication/routing | [SF-078](https://github.com/tyler-suifleet/suifleet/issues/132) | SF-032/057 | Actual messages/attributes, caller/topic grants, throttling, ambiguous responses and bounded retries |
| Local support debug console | [SF-079](https://github.com/tyler-suifleet/suifleet/issues/133) | SF-019/027/053 | Local authenticated API/UI, scoped operations, origin/CSRF defenses, redaction, bounded queries and audit |

Linux x86_64/aarch64/arm remains the initial prototype phase. SF-074 adds the
[Windows local operator runtime](WINDOWS_RUNTIME.md); catalog/chain/rich IPC
support remains unsupported. Platform gaps are not permanent exclusions. SF-076 adds the native
32-bit Linux x86 daemon, packaging and catalog selection
([LINUX_X86.md](LINUX_X86.md)); managed adapters there remain outside the phase. Each
platform ticket must declare exact supported OS/ABI versions and exercise its own
runner; a Linux x86_64 result cannot establish another platform's support.
Platform scripts retain OS installation, reboot and firmware recovery ownership.

SF-072 now provides a [native H.264/Matroska media adapter](KINESIS_VIDEO.md) with
[local runtime evidence](validation/sf072-video.json). Its protocol-specific
configuration and media contract supplement the reference delivery model below.
Full AWS connector control-plane compatibility and live AWS acceptance remain
roadmap/unrun; local execution does not establish whole-catalog parity.

## Executable functional contracts, version 1

[remaining-roadmap-cases.json](../interfaces/compat/remaining-roadmap-cases.json)
contains explicit request/expected-result vectors. Each request carries integer
`version: 1` and boolean `authorized: true`. Unknown versions (including boolean
`true`) return `unsupported-version` before any action; missing/false grants
return `denied` before inspecting payloads. The pure reference evaluator in
[greengrass_roadmap.py](../scripts/greengrass_roadmap.py) performs no process,
network, filesystem or credential operation. Fixture fields represent already
validated internal inputs; this is not an untrusted wire request schema.

These are minimal proposed SuiFleet functional subcontracts. They do not specify
AWS wire compatibility, SDK behavior or the complete service protocol. Each
implementation must pin/review its upstream semantic references, add its complete
input/configuration schema and protocol-specific vectors, and execute both these
subcontracts and the actual-process tests above. Changes to these decisions require
a new contract version and reviewed migration fixtures, not silently changed v1
expectations. No AWS upstream behavior is newly asserted by these local contracts.

- **Lambda.** `mode` is `on-demand` or `long-lived`. Elapsed milliseconds at or above
  the configured timeout returns `timeout`; active invocations at the concurrency
  bound return `busy`. Exact source/subject subscription matches produce sorted,
  unique targets and preserve the JSON payload. No match produces no targets.
  Process creation/reuse, handler failure envelopes and child termination require
  SF-069 runtime tests; a routing decision is not an invocation.
- **Industrial/SiteWise.** A measurement carries a numeric non-boolean value,
  nonnegative integer nanosecond timestamp and `GOOD`, `BAD` or `UNCERTAIN` quality.
  Preserve quality and time. Unknown quality or invalid measurement is rejected.
  SiteWise requires a configured alias and applies `value * scale + offset`.
  Polling/security, OPC UA types/subscriptions, property mappings, aggregation,
  partial service errors and protocol-specific validation remain SF-070/071 work.
- **Video/Firehose/SNS delivery.** Ordered local record sequence numbers begin at
  zero; the initial durable checkpoint is -1. Sum of record bytes above the
  configured bound returns backpressure without moving the checkpoint. Advance
  only across a contiguous prefix with explicit `ack` results. A rejected,
  ambiguous or missing sequence retains that record and all later records,
  even when a later result is acknowledged. Retry after restart begins after the
  durable checkpoint. Delivery is at least once: a lost response may duplicate
  delivery. This local checkpoint contract does not conflate the three service
  protocols. Media fragments/ACKs and exact Firehose/SNS limits, serialization,
  attributes, retry budgets and credentials require their separate implementations.
- **ML.** Require authenticated model digest equality, pinned runtime equality and
  exact input shape before inference. Compare actual/expected output vectors of
  equal length using absolute per-element tolerance. A digest, shape, runtime or
  output mismatch fails the corresponding gate. This comparison does not execute
  inference; classifier/detector fixtures must declare preprocessing, tensor types,
  labels/boxes and tolerances against the actual pinned runtime in SF-073.
- **Platforms.** Reject mismatched artifact/platform pairs. A platform cannot be
  eligible without process containment and private device identity. Interrupted
  lifecycle work requires recovery/inspection, not replay of destructive work.
  These boolean preconditions are test obligations for real service runners,
  not probes that demonstrate the current daemon works on those platforms.
- **Console.** Require the configured origin and an explicit operation grant.
  Only `component`, `version` and `health` fields can leave this reference view;
  key/token/other fields are omitted. SF-079 must implement authentication,
  operation authorization, CSRF protection where applicable, audit and bounded
  queries. A configured origin alone is never authentication.

## Evidence and closure

SF-077 adds the [native Firehose exporter](FIREHOSE.md), with actual SDK HTTP
emulator and durable daemon stream evidence. The pure reference fields below
remain specification-only; live AWS and physical target acceptance are unrun.

SF-076 adds the [32-bit Linux x86 runtime](LINUX_X86.md): an i686 build and
release bundle, `x86` catalog selection and the real-daemon suites run in a
32-bit userland. Its receipt is separate evidence; the reference vectors below
remain specification checks.

SF-073 adds [native reproducible ML components](ML_COMPONENTS.md), with original
models and actual TensorFlow Lite software-runner inference tests. This separate
implementation evidence does not turn the AWS-named catalog or the reference
evaluator below into an AWS-compatible runtime.

SF-075 adds a [native macOS runtime](MACOS_RUNTIME.md): a LaunchDaemon whose
launchd coalition contains component descendants, peer-UID IPC, private key
checks and the catalog OS `macos`, exercised by real launchd service, crash,
interrupted-install and permission tests on a hosted macOS runner. It is not
Greengrass platform compatibility, and the `macos` scope below stays a reference
model.

Run `make test-compat`. It checks the existing catalog and reference suites plus
76 remaining-roadmap vectors and negative ownership/publication/evidence guards.
`python3 scripts/greengrass_roadmap.py` runs just the new roadmap checks. The
checker rejects missing scope ownership, dependency/acceptance drift, unpublished
Project records, unmapped roadmap catalog/platform gaps and runtime/parity claims.

All new scope `runtime_execution` and `live_execution` fields remain `pending`.
Reference-model agreement establishes internal specification consistency only.
An implementation receipt must identify exact code/version, runner, fixture
version, command/results and limitations; service/hardware evidence belongs in a
separate receipt. No closed-source AWS component is redistributed or executed.
Clean-room functional alternatives and licensed runtime/model packaging must not
be relabeled native AWS recipe/API compatibility.

No live AWS credentials, paid services or mutations are authorized by this plan.
The standing Sui testnet grant covers no AWS account. Physical Pi boot/network,
power-loss and device-specific results remain separately tracked in
[SF-031](https://github.com/tyler-suifleet/suifleet/issues/32); no refreshed image or
physical result is claimed. Software correctness, source/security review,
meaningful local/emulator tests and normal authorized automatic CI remain gates.
