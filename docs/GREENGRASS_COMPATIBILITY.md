# Greengrass compatibility specification

The [SF-064 remaining functional roadmap](GREENGRASS_REMAINING_ROADMAP.md) defines versioned executable subcontracts and maps every selected remaining scope to published implementation tickets SF-069–SF-079. These remain pending implementations, not runtime or live acceptance.

SF-028 defines what "Greengrass compatible" may mean for SuiFleet. It also proposes adapter contracts for messaging, shadows, streams and client devices, with executable fixtures. This is a specification. It is not a parity claim and implements no runtime adapter. The [parity audit](GREENGRASS_PARITY_AUDIT.md) and [ticket map](GREENGRASS_TICKET_COVERAGE.md) remain the roadmap.

The machine-readable source of truth is [`interfaces/compat/greengrass-compat.json`](../interfaces/compat/greengrass-compat.json). Statuses there describe the commit that contains the file. Work in open PRs counts as "proposed" until it is merged.

AWS reference: Greengrass V2 classic nucleus 2.x documentation, checked 2026-10-15. Nucleus lite is out of scope. Component references are client device auth 2.5.x, MQTT bridge 2.3.x, shadow manager IPC and the stream manager 2.3.x SDK data model. Sources are listed in the matrix.

## Compatibility levels

Each capability has a separate status for each level. A higher level never follows from a lower one.

| Level | Meaning | Default position |
|---|---|---|
| `functional` | Equivalent behaviour through SuiFleet's own chain, recipe and local APIs | The product goal |
| `aws_recipe` | AWS recipe documents are translated, or rejected with a stated reason | Proposed subset (SF-037); never silently reinterpreted |
| `aws_api` | Unmodified AWS IPC/SDK/CLI/cloud-API clients work | `undecided` unless scoped: SF-038 adapters with implementation tickets are `proposed`, SF-064 gaps are `roadmap` |

Statuses: `implemented`/`partial` (merged, with cited local evidence), `proposed`, `undecided`, `roadmap` (tracked missing work, not an exclusion), `excluded` (approved decision only) and `not-applicable`. The checker enforces several rules:

- `partial` and `implemented` must cite files that exist.
- No row may claim `aws_api` or `aws_recipe` `partial`/`implemented` without a suite whose `runtime_execution` is `passed`.
- A `passed` suite needs an existing runtime receipt.
- `parity_claim` must be `false`.
- Every ticket key must exist in the backlog.

## Supported platforms

| Platform | AWS `Platform` | Native catalog | Status |
|---|---|---|---|
| Linux x86_64 | `linux`/`amd64` | `linux`/`x86_64` | Partial: daemon, simulator and localnet evidence |
| Linux aarch64 | `linux`/`aarch64` | `linux`/`aarch64` | Partial: Yocto QEMU; physical Pi pending in SF-031 |
| Linux 32-bit ARM | `linux`/`arm` | `linux`/`arm` | Proposed: selection vectors only. `architecture.detail` is rejected |
| Linux x86 | `linux`/`x86` | none | Roadmap gap (SF-064), outside the SF-038 initial Linux phase. Translation omits these manifests |
| Windows, macOS | `windows`, `darwin` | none | Roadmap gap (SF-064), outside the SF-038 initial Linux phase. Translation omits these manifests |

## Current APIs versus proposed contracts

The following is merged today and is the only thing the daemon and frontend execute:

- **Native recipes** (`interfaces/recipe.schema.json`) are versions 1.0/1.1, with one authenticated artifact, empty configuration and native dependency ranges. The lifecycle is `install`, plus the SF-014 supervised `run`/`shutdown`/`readiness`/`restart` ([OPERATIONS.md](OPERATIONS.md#supervised-components-sf-014)). Commands use the shell-quoted substitutions `{artifact}`, `{artifacts}` and `{work}` (`{work}` only in supervised commands), and `{pid}` in readiness/shutdown.
- **The catalog** has platform manifests `linux|*` × `x86_64|aarch64|arm|*`, selected by first match.
- **Native IPC v1** provides UID-authenticated component APIs, operator management and bounded volatile messaging ([COMPONENT_IPC.md](COMPONENT_IPC.md)). Shared-daemon-user components cannot receive individual ACLs; separate platform Unix identities are required. There is no AWS IPC wire compatibility, durable pub/sub, MQTT broker, shadow, stream, client-device or AWS recipe support.

Everything else here is proposed. The fixtures pin its semantics. The owning tickets implement them and may revise them through review. Implementers must not treat this document as a published runtime API, and must not change merged APIs to fit it by assumption.

## AWS recipe translation subset (SF-037)

The input is an AWS recipe, already parsed from JSON or YAML into the same value, plus an operator-supplied artifact binding: `Uri` → `{artifactBlobId, artifactSha256}`. A Walrus upload must be authenticated by SHA-256. AWS `s3://` URIs are never fetched. The output is either:

- `translate`, with ordered native catalog manifests `{os, arch, recipe}`, the indexes of omitted manifests and the metadata reported as informational; or
- `defer`, which lists the runtime tickets that must merge first. Until a later reviewed change re-specifies these cases, the translator must reject them; or
- `reject`, which lists every reason.

| AWS field | Rule |
|---|---|
| `RecipeFormatVersion` | Must be `2020-01-25` |
| `ComponentType`, `ComponentSource` | Generic only; Lambda, plugin and nucleus are rejected (SF-038) |
| `ComponentName`, `ComponentVersion` | Native name grammar; SemVer |
| `ComponentDescription`, `ComponentPublisher` | Description copied; publisher reported as informational |
| `ComponentConfiguration.DefaultConfiguration` | Empty only. Nonempty is deferred to SF-015; `accessControl` is deferred to SF-027 |
| `ComponentDependencies` | Native comparator grammar, HARD/SOFT. x-ranges, hyphen ranges, `\|\|` and `aws.greengrass.*` dependencies are rejected |
| `Manifests[].Platform` | `os`/`architecture` exact or `*` only; `amd64`→`x86_64`. Regex, `architecture.detail`, `runtime` and custom keys are rejected. Windows/macOS/x86 manifests are omitted and reported. No supported manifest, or a duplicate pair, is rejected |
| `Manifests[].Artifacts` | Exactly one bound artifact. `Unarchive` other than `NONE`, or non-default `Permission`, is rejected. An AWS `Digest` must be SHA-256 and equal the binding |
| `install`, `run`, `shutdown` | String or `{Script}`, mapped to the native command of the same name. `run`/`shutdown` are reported as informational, because the native restart policy (`on-failure`, backoff, Broken after 5 restarts in 10 minutes) and stop timeout replace AWS `recover` retries and step timeouts. `shutdown` without `run` is rejected |
| `bootstrap`, `startup`, `recover`, `uninstall` | Rejected: no native equivalent |
| Step `Timeout`, `Setenv`, `Lifecycle.Setenv`, `RequiresPrivilege: true`, `Skipif`, `BootstrapOnRollback` | Rejected |
| Global `Lifecycle` / `Selections` | Rejected |
| `{artifacts:path}/<Uri basename>` | Becomes `{artifact}`. Any other `{artifacts:…}` use, including `decompressedPath`, is rejected |
| `{work:path}` | Becomes `{work}` in `run`/`shutdown`; rejected in `install` |
| `{configuration:…}` | Deferred to SF-015 |
| `{dep:…:…}`, `{iot:thingName}`, `{kernel:rootPath}`, unknown namespaces | Rejected. No other identity is substituted |
| Variables in a script containing quotes or backslashes; literal `{artifact}`/`{artifacts}`/`{work}`/`{pid}` | Rejected, because native shell-quoted substitution would change their meaning |

Translation produces native recipe 1.1 when dependencies exist and 1.0 otherwise. The current daemon validator (`compat_tests.rs`) and frontend validator (`test_greengrass_compat.cjs`) check two things: every AWS fixture is rejected as a native recipe, and every expected translated manifest is accepted.

## Adapter designs

Everything below is proposed. Each adapter runs as a supervised local service behind the SF-027 authenticated local API. It is authorized per component, deny-by-default, and keeps chain/Walrus for authority and artifacts only. High-volume or private data such as messages, shadow documents and streams never goes on chain.

### Messaging (SF-032)

- **Topics follow MQTT 3.1.1/5 §4.7.** `+` matches one level and `#` matches the remaining levels; `#` is only valid as the last level. Topic names may not contain wildcards. Filters that begin with a wildcard do not match `$` topics. Matching is case-sensitive, and empty levels are significant.
- **Local pub/sub authorization mirrors AWS `aws.greengrass.ipc.pubsub` policies.** Operations are `aws.greengrass#PublishToTopic`, `aws.greengrass#SubscribeToTopic` or `*`. A resource is a glob in which only `*` is special. `+` and `#` are literal in resources, so a grant of `test/topic/#` permits subscribing to that filter, not to `test/topic/x`. Any matching policy grants.
- **Brokers and bridging.** A standard MQTT 3.1.1/5 broker serves LAN clients. Bridge mappings are `{topic, source, target, targetTopicPrefix?}`, with source and target taken from `LocalMqtt` and `Pubsub`. Source and target must differ. Any cloud target is a separately approved adapter. The spool is bounded and on disk. The bridge forwards QoS 1 at least once with per-mapping ordering, and on overflow it applies backpressure and an explicit drop counter, never silent loss.

### Shadows (SF-033)

- **Document semantics follow AWS IoT Device Shadow.** A request has the shape `{state:{desired?,reported?}, version?, clientToken?}`, and unknown request keys are invalid. Updates merge recursively. Arrays are values that are replaced whole and may not contain `null`. A `null` value deletes its key, and a `null` section deletes the section. Objects left empty are removed.
- **Delta and versions.** The delta contains the desired keys that differ from reported, giving the path from the root; keys that exist only in reported are ignored. Values are compared as JSON types, so `true` differs from `1` and `[false]` differs from `[0]`. Each accepted change increments the version by 1. A supplied version must equal the current one, or the result is `conflict` (AWS 409). A supplied version, on update or delete, must be a JSON integer of at least 1; anything else, including a boolean, a string or a non-integer number, is `invalid` and checked before `not-found` or `conflict`.
- **Limits.** Depth may not exceed 8 in either section. The section object is level 1, and every nested object or array adds a level, including objects inside arrays and arrays inside arrays; this array rule is the proposed SuiFleet convention. A rejected request leaves the document and version unchanged. `clientToken` may not exceed 64 bytes, and the serialized state may not exceed 8 KiB (`too-large`). Shadow names are 1–64 characters of `[A-Za-z0-9:_-]`, and `""` is the classic shadow.
- **Proposed differences from AWS.**
  - Deleting a shadow increments the version, and a recreated shadow continues the sequence. There is no 48-hour reset.
  - Cloud synchronization is a selected adapter with an explicit conflict policy.

### Streams (SF-034)

- **Stream definitions.** Each stream has a name of 1–255 characters (`[A-Za-z0-9 ,._-]`), a byte bound of at least 1 KiB, a `strategy_on_full` of `RejectNewData` or `OverwriteOldestData`, and a persistence of `File` (the default) or `Memory`.
- **Sequence numbers.** Numbers start at 0 for each stream. They are gap-free at append, monotonic, and never reused, even after eviction or restart.
- **Reads.** A read below the oldest retained number returns `evicted` with the oldest number, so consumers detect loss. Fewer than `min` messages returns `not-enough-messages`.
- **Restart.** A restart clears `Memory` streams and leaves `File` streams intact.
- **Export.** Export checkpoints are idempotent, never move backwards and cannot exceed the newest sequence number. Export destinations, batching and credentials belong to SF-034/SF-038.
- **Proposed difference from AWS.** Eviction is per message, whereas AWS evicts whole segments.

### Client devices (SF-035)

- **Groups and policies.** These use the `aws.greengrass.clientdevices.Auth` 2.5.x shape. A selection rule has the form `thingName: pattern [OR …]`, where `*` may appear only at the start and/or end and `\:` escapes a colon. Policy statements combine `mqtt:connect` with `mqtt:clientId:`, `mqtt:publish` with `mqtt:topic:`, and `mqtt:subscribe` with `mqtt:topicfilter:`. `*` acts as a glob, and MQTT wildcards in topic filters are matched literally. The union of all matching groups applies, and anything not granted is denied.
- **Identity.** A client identity is SuiFleet-enrolled. It is denied when unknown, revoked, or at or after its expiry time.
- **Proposed hardening relative to AWS.** A client may connect only with a client ID equal to its own identity. Certificate issuance, rotation and offline cache expiry belong to SF-035.

## Acceptance suite

```bash
make test-compat
```

This runs `python3 scripts/greengrass_compat.py`, the checker's negative tests and the frontend validator check. The checker also validates the SF-038 [managed-component catalog](GREENGRASS_MANAGED_COMPONENTS.md). `make edge-test` runs the Rust current-API check.

| Suite | Cases evaluated by the reference model | Current-API execution | Runtime execution |
|---|---|---|---|
| `aws-recipe-cases.json` | 49 recipes | Daemon and frontend validators (passing) | Pending SF-037 |
| `pubsub-cases.json` | 53 filter/topic/match/ACL checks | none | Pending SF-027, SF-032 |
| `shadow-cases.json` | 54 steps across 10 sequences | none | Pending SF-027, SF-033 |
| `stream-cases.json` | 45 steps across 5 sequences | none | Pending SF-027, SF-034 |
| `client-device-cases.json` | 16 authorization requests | none | Pending SF-032, SF-035 |
| `managed-dependency-cases.json` | 47 managed-dependency classifications | none | Pending SF-037 |
| `credential-provider-cases.json` | 65 steps across 7 sequences | none | Pending SF-057 |
| `cloudwatch-metrics-cases.json` | 90 steps across 8 sequences | none | Pending SF-059 |
| `cloudwatch-logs-cases.json` | 69 steps across 8 sequences | none | Pending SF-060 |

`scripts/greengrass_compat.py` contains small **specification reference models**. They make each expected value checkable and keep the fixtures internally consistent. They are not the daemon, a translator or an adapter, and agreement with them proves nothing about runtime conformance. Each owning ticket must execute the same fixture files against its real implementation. That ticket then sets the suite to `passed` with a committed runtime receipt, and only after that may it raise any `aws_*` status. Fixtures may be corrected in review if they contradict AWS behaviour or an approved design. Correct them deliberately; never loosen them to make an implementation pass.

## Recommendations for Codex

These are recommendations only. Codex decides on approval and on any ticket changes.

1. **SF-037.** Adopt the recipe subset and fixtures above as acceptance. Add YAML parsing, translator CLI/UI output and per-case migration guidance. Re-specify the deferred cases only after SF-015/SF-027 merge. Bootstrap/startup/recover/uninstall need a separate native-lifecycle decision before they can be translated.
2. **SF-032/033/034/035.** Use the matching fixture files as required runtime acceptance, plus restart, disk-full, concurrency and authorization-failure tests on real processes.
3. **`aws_api` levels** are `undecided`. Recommendations:
   - Exclude AWS control-plane API emulation.
   - Defer AWS IPC wire/SDK compatibility until a native SF-027 API exists.
   - AWS cloud targets (IoT Core bridge, shadow sync, Kinesis/S3 export, token exchange, CloudWatch) are SF-038 adapters with implementation tickets SF-057 to SF-063.
4. **SF-038.** Maps AWS-managed components, Lambda and Windows/macOS/x86 in [GREENGRASS_MANAGED_COMPONENTS.md](GREENGRASS_MANAGED_COMPONENTS.md). Lambda, industrial, ML example and non-initial platform gaps are SF-064 roadmap work, not approved exclusions.
