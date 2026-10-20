# Greengrass compatibility specification

The [SF-064 remaining functional roadmap](GREENGRASS_REMAINING_ROADMAP.md) defines versioned executable subcontracts and maps every selected remaining scope to published implementation tickets SF-069–SF-079. These remain pending implementations, not runtime or live acceptance.

SF-028 defines what "Greengrass compatible" may mean for SuiFleet. It also proposes adapter contracts for messaging, shadows, streams and client devices, with executable fixtures. It is a specification, not a parity claim. Of the runtime work it describes, only the SF-037 recipe translator is implemented; no adapter is. The [parity audit](GREENGRASS_PARITY_AUDIT.md) and [ticket map](GREENGRASS_TICKET_COVERAGE.md) remain the roadmap.

The machine-readable source of truth is [`interfaces/compat/greengrass-compat.json`](../interfaces/compat/greengrass-compat.json). Statuses there describe the commit that contains the file. Work in open PRs counts as "proposed" until it is merged.

AWS reference: Greengrass V2 classic nucleus 2.x documentation, checked 2026-10-15. Nucleus lite is out of scope. Component references are client device auth 2.5.x, MQTT bridge 2.3.x, shadow manager IPC and the stream manager 2.3.x SDK data model. Sources are listed in the matrix.

## Compatibility levels

Each capability has a separate status for each level. A higher level never follows from a lower one.

| Level | Meaning | Default position |
|---|---|---|
| `functional` | Equivalent behaviour through SuiFleet's own chain, recipe and local APIs | The product goal |
| `aws_recipe` | AWS recipe documents are translated, or rejected with a stated reason | Partial: the SF-037 subset is implemented; nothing is silently reinterpreted |
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
| Linux x86 (32-bit) | `linux`/`x86` | `linux`/`x86` | Partial: i686 daemon, release bundle and catalog selection with 32-bit software-runner evidence ([LINUX_X86.md](LINUX_X86.md)). Outside the SF-038 initial Linux phase; translation still omits these manifests |
| Windows | `windows` | none | Roadmap gap (SF-064, SF-074), outside the SF-038 initial Linux phase. Translation omits these manifests |
| macOS | `darwin` | `macos`/`aarch64` | Partial: native LaunchDaemon runtime on a hosted macOS 15 arm64 runner ([MACOS_RUNTIME.md](MACOS_RUNTIME.md), SF-075); not a Greengrass platform. Translation still omits `darwin` manifests |

## Current APIs versus proposed contracts

The following is merged today and is the only thing the daemon and frontend execute:

- **Native recipes** (`interfaces/recipe.schema.json`) are versions 1.0–1.2, with one authenticated artifact, native dependency ranges (1.1) and SF-015 configuration (1.2: `defaults`, `merge`, `reset`, `confidential`, `validate`). The lifecycle is `install`, plus the SF-014 supervised `run`/`shutdown`/`readiness`/`restart` ([OPERATIONS.md](OPERATIONS.md#supervised-components-sf-014)). Commands use the shell-quoted substitutions `{artifact}`, `{artifacts}`, `{configuration}` and `{work}` (`{work}` only in supervised commands), and `{pid}` in readiness/shutdown. A recipe with neither `install` nor `run` hands its artifact to the platform updater.
- **The catalog** has platform manifests `linux|*` × `x86_64|aarch64|arm|x86|*`, selected by first match (`x86` is 32-bit i686, SF-076).
- **Native IPC v1** provides UID-authenticated component APIs, operator management and bounded volatile messaging ([COMPONENT_IPC.md](COMPONENT_IPC.md)). Shared-daemon-user components cannot receive individual ACLs; separate platform Unix identities are required. Durable pub/sub with a bridge to a local broker (SF-032, [MESSAGING.md](MESSAGING.md)), streams (SF-034, [STREAMS.md](STREAMS.md)) and shadows (SF-033, [SHADOWS.md](SHADOWS.md)) are native IPC operations.
- **AWS recipe translation** (SF-037, below) turns the supported Greengrass V2 recipe subset into native catalog manifests, offline, in the dApp and a CLI. The daemon itself never reads AWS recipes.

There is no AWS IPC wire compatibility, embedded MQTT broker or client-device support. Everything else here is proposed. The fixtures pin its semantics. The owning tickets implement them and may revise them through review. Implementers must not treat this document as a published runtime API, and must not change merged APIs to fit it by assumption.

## AWS recipe translation (SF-037)

The translator is [`dapp/src/lib/greengrass-recipe.ts`](../dapp/src/lib/greengrass-recipe.ts). Operators use it in two places:

- the **Import AWS Greengrass recipe** panel on the dApp's Components page, which uploads the bound artifacts and translated recipes and registers an immutable catalog version (accepted with the Slush extension on Sui and Walrus testnet: [receipt](validation/sf037-recipe-translation.json), `extension_wallet_testnet`);
- the offline CLI:

  ```bash
  node scripts/greengrass_translate.cjs recipe.yaml --bind s3://bucket/app/1.0.0/app.tar=BLOB_ID:SHA256
  ```

  It prints the result as JSON, with guidance for every reason and reported difference. It exits with 0 when the recipe translates, 1 when it is rejected and 2 on a usage error.

The input is one JSON or YAML recipe document, plus an operator-supplied artifact binding: `Uri` → `{artifactBlobId, artifactSha256}`. A Walrus upload must be authenticated by SHA-256. AWS `s3://` URIs are never fetched. The output is one of two outcomes:

- `translate`, with:
  - the ordered native catalog manifests `{os, arch, recipe}`;
  - the indexes of omitted manifests;
  - `informational`, the reported differences from AWS behaviour. The dApp requires the operator to acknowledge these before it uploads anything.
- `reject`, which lists every reason, each with migration guidance.

Either outcome may also carry:

- `managed_dependencies`, the SF-038 classification of each AWS-managed dependency;
- `ipc_acl`, the operator IPC ACL equivalent to the recipe's `accessControl`.

The earlier `defer` outcome is gone: SF-015 and SF-027 have merged, and the cases that waited on them are re-specified below.

**Parsing.** Text that is valid JSON is read as JSON. Greengrass reads YAML with a YAML 1.1 parser, so YAML is accepted only if YAML 1.1 and YAML 1.2 produce the same JSON value. A YAML 1.1 timestamp is the one exception: it reads as the string it spells, so an unquoted `RecipeFormatVersion: 2020-01-25` is accepted. The parser rejects the following, each with its own reason:

- plain scalars whose meaning differs between YAML 1.1 and 1.2: `yes`/`no`/`on`/`off`, `0755`, `1:20` and `<<` merge keys;
- duplicate keys, in both JSON and YAML;
- tags, anchors and aliases, directives, non-string keys, and non-finite or unsafe numbers;
- multiple documents, and syntax errors.

Field names are exact: Greengrass's case variants, such as `Install`, are rejected as unknown, never guessed.

| AWS field | Rule |
|---|---|
| `RecipeFormatVersion` | Must be `2020-01-25` |
| `ComponentType`, `ComponentSource` | Generic only. Lambda, plugin and nucleus types are rejected (SF-038) |
| `ComponentName`, `ComponentVersion` | Native name grammar; SemVer |
| `ComponentDescription`, `ComponentPublisher` | The description must be a string and is copied. The publisher is reported |
| `ComponentConfiguration.DefaultConfiguration` | Becomes native recipe 1.2 `configuration.defaults`, which is reported. The rules are under [Configuration and permissions](#configuration-and-permissions) |
| `accessControl` | Rejected (`ipc-access-control`); the equivalent operator ACL is reported. See [Configuration and permissions](#configuration-and-permissions) |
| `ComponentDependencies` | Every name is kept as given, including `__proto__`. Native comparator grammar; `DependencyType` must be HARD or SOFT. Every dependency becomes native HARD (the default), also AWS SOFT: AWS installs and starts both types first and SOFT only drops restart coupling, while a native SOFT dependency may be omitted when unavailable. The `ComponentDependencies` difference is reported: no AWS HARD restart coupling (a dependent is not restarted or stopped when a dependency restarts, errors or is replaced), and a dependent still starts if a dependency fails to become ready. x-ranges, hyphen ranges and `\|\|` are rejected. AWS-managed names (`aws.greengrass.*`, `aws.iot.*`, `variant.*`) are classified with the SF-038 catalog. A `replace` (the daemon provides the function, for example `aws.greengrass.Nucleus`) is dropped and reported. Every other classification is rejected (`aws-managed-dependency`) and reported in `managed_dependencies` |
| `Manifests[].Platform` | `os`/`architecture` exact or `*` only; `amd64`→`x86_64`. Regex, `architecture.detail`, `runtime` and custom keys are rejected. Windows/macOS/x86 manifests are omitted and reported. No supported manifest, or a duplicate pair, is rejected |
| `Manifests[].Artifacts` | Exactly one bound artifact. `Unarchive` other than `NONE`, or non-default `Permission` (Read `OWNER`, Execute `NONE`), is rejected. An AWS `Digest` must be SHA-256 and equal the binding |
| `install`, `run`, `shutdown` | String or `{Script}` (with `RequiresPrivilege: false` at most), mapped to the native command of the same name. Each is reported; see [Reported differences](#reported-differences). `shutdown` without `run` is rejected. An empty or whitespace-only script is rejected (`empty-step`) |
| A manifest with neither `install` nor `run` | Rejected (`no-lifecycle-command`). Natively it would hand the artifact to the platform updater |
| `bootstrap`, `startup`, `recover`, `uninstall` | Rejected: no native equivalent |
| Step `Timeout`, `Setenv`, `Lifecycle.Setenv`, `RequiresPrivilege` other than `false` (including the string `"true"`), `Skipif`, `BootstrapOnRollback` | Rejected |
| Global `Lifecycle` / `Selections` | Rejected |
| `{artifacts:path}/<Uri basename>` | Becomes `{artifact}`. Any other `{artifacts:…}` use, including `decompressedPath`, is rejected |
| `{work:path}` | Becomes `{work}` in `run`/`shutdown`; rejected in `install` |
| `{configuration:/pointer}` | Rejected. Native commands receive the whole configuration as the `{configuration}` file, never inline values |
| `{dep:…:…}`, `{iot:thingName}`, `{kernel:rootPath}`, unknown namespaces | Rejected. No other identity is substituted |
| Nucleus environment in scripts (`SVCUID`, `AWS_GG_*`, `AWS_IOT_THING_NAME`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `GG_ROOT_CA_PATH`, `AWS_CONTAINER_*`) | Rejected (`aws-environment-variable`); the daemon clears the environment |
| Variables in a script containing quotes or backslashes; literal `{artifact}`/`{artifacts}`/`{work}`/`{pid}`/`{configuration}` | Rejected, because native shell-quoted substitution would change their meaning |

Translation produces native recipe 1.2 when there is a configuration, 1.1 when there are native dependencies and 1.0 otherwise.

### Configuration and permissions

**Default configuration.** A non-empty `DefaultConfiguration` becomes `configuration.defaults`. Native commands receive it as the 0600 `{configuration}` file, and `run`/`shutdown` also as `$SUIFLEET_CONFIGURATION`; there is no IPC `GetConfiguration`. Three cases are rejected:

- a manifest with no `install` or `run` command to receive the configuration;
- more than 4096 values, or a depth greater than 32;
- keys that differ only in case. The Greengrass nucleus configuration store looks keys up case-insensitively, but native keys are case-sensitive.

Per-deployment AWS `reset`/`merge` updates correspond to native configuration deployments. Reset runs before merge in both. On an upgrade, native configuration keeps the device's values and fills only missing keys from the new version's defaults. The AWS guide does not specify how a changed default combines with an existing value, so check upgrades that change defaults.

**Access control.** `accessControl` authorizes the AWS IPC protocol, which SuiFleet does not implement. Native IPC ACLs are operator-owned daemon configuration for separately provisioned Unix users; a recipe never grants them. Recipes with `accessControl` are therefore rejected (`ipc-access-control`). The translator still reports the equivalent operator ACL (`ipc_acl`, shown as TOML in the dApp and CLI) for the component once it is ported to the native SDK:

- `config` and `health` are always included. Greengrass lets every component read its own configuration and report its state.
- Exact `aws.greengrass.ipc.pubsub` topics map to `publish` and to `subscribe`/`receive`.
- Glob resources and every other IPC service are listed as `unmapped`.

**Execution identity.** `RequiresPrivilege` other than `false` is rejected; components run as the daemon user. Non-default artifact `Permission` is rejected.

### Reported differences

Each translated field below runs with a difference from AWS. The translator reports it in `informational`, with guidance, rather than hiding it:

| Field | Native behaviour |
|---|---|
| `Lifecycle.install` | Runs in the version's artifact directory, which is temporary for install-only components, not the AWS work folder (`$PWD`). Runs as the daemon user with a cleared environment. Bounded by the operator's `apply.timeout_seconds`, not the AWS 120 s default |
| `Lifecycle.run` | Supervised in the component work directory, which is `{work:path}` as in AWS, with no timeout. Restarted on failure with backoff and marked Broken after 5 restarts in 10 minutes, replacing AWS `recover`. Runs as the daemon user with a cleared environment |
| `Lifecycle.shutdown` | Bounded by the operator's component stop timeout, not the AWS 15 s default |
| `ComponentConfiguration.DefaultConfiguration` | Delivered as a file instead of over IPC. Upgrade rule as above |
| `ComponentDependencies.<managed>` | Dropped, because the daemon replaces it (SF-038 `replace`) |
| `ComponentPublisher` | Not stored; the catalog owner identifies the publisher |

**Corrections to the SF-028 fixtures.** Three SF-028 expectations changed:

- An artifact-only manifest with an empty `Lifecycle` was expected to translate. It is now rejected, because natively it would invoke the platform updater (`scripts/test_greengrass_runtime.py` demonstrates this).
- `install` is now reported.
- The deferred configuration and `accessControl` cases now have the outcomes described above.

## IPC and SDK compatibility boundaries

- **AWS IPC/SDK.** Unmodified AWS IPC clients do not work. This covers the event-stream protocol, `AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT` and `SVCUID`, the Greengrass component SDKs, and `greengrass-cli`. The `aws_api` level stays `undecided`.
- **Porting a component.** A translated component that uses AWS IPC must be ported to the [native IPC v1 SDK](COMPONENT_IPC.md):

  | AWS IPC | Native IPC v1 |
  |---|---|
  | `GetConfiguration` | `config`, or the configuration file |
  | `UpdateState` | `health` |
  | `PublishToTopic` / `SubscribeToTopic` on exact topics | `publish` / `receive` |

  The component must run as a separately provisioned Unix user, and the operator adds its ACL.
- **Not available natively yet.** Wildcard subscriptions, MQTT/IoT Core, shadows, streams, secrets and token exchange are SF-032 to SF-035, SF-022 and SF-057.
- **Lambda.** Lambda functions and subscriptions are SF-064 roadmap work.
- **Recipe compatibility is the `aws_recipe` level only.** A translated recipe runs its scripts with the stated differences. It does not make the component's own AWS SDK calls work.

## Adapter designs

Everything below is proposed. Each adapter runs as a supervised local service behind the SF-027 authenticated local API. It is authorized per component, deny-by-default, and keeps chain/Walrus for authority and artifacts only. High-volume or private data such as messages, shadow documents and streams never goes on chain.

### Messaging (SF-032)

Implemented for local pub/sub and the LocalMqtt/Pubsub bridge: see [MESSAGING.md](MESSAGING.md). The rest of this section is the specification it follows.

- **Topics follow MQTT 3.1.1/5 §4.7.** `+` matches one level and `#` matches the remaining levels; `#` is only valid as the last level. Topic names may not contain wildcards. Filters that begin with a wildcard do not match `$` topics. Matching is case-sensitive, and empty levels are significant.
- **Local pub/sub authorization mirrors AWS `aws.greengrass.ipc.pubsub` policies.** Operations are `aws.greengrass#PublishToTopic`, `aws.greengrass#SubscribeToTopic` or `*`. A resource is a glob in which only `*` is special. `+` and `#` are literal in resources, so a grant of `test/topic/#` permits subscribing to that filter, not to `test/topic/x`. Any matching policy grants.
- **Brokers and bridging.** A standard MQTT 3.1.1/5 broker serves LAN clients. Bridge mappings are `{topic, source, target, targetTopicPrefix?}`, with source and target taken from `LocalMqtt` and `Pubsub`. Source and target must differ. Any cloud target is a separately approved adapter. The spool is bounded and on disk. The bridge forwards QoS 1 at least once with per-mapping ordering, and on overflow it applies backpressure and an explicit drop counter, never silent loss.

### Shadows (SF-033)

Implemented for local shadows and an HTTP(S) sync adapter: see [SHADOWS.md](SHADOWS.md). The rest of this section is the specification it follows.

- **Document semantics follow AWS IoT Device Shadow.** A request has the shape `{state:{desired?,reported?}, version?, clientToken?}`, and unknown request keys are invalid. Updates merge recursively. Arrays are values that are replaced whole and may not contain `null`. A `null` value deletes its key, and a `null` section deletes the section. Objects left empty are removed.
- **Delta and versions.** The delta contains the desired keys that differ from reported, giving the path from the root; keys that exist only in reported are ignored. Values are compared as JSON types, so `true` differs from `1` and `[false]` differs from `[0]`. Each accepted change increments the version by 1. A supplied version must equal the current one, or the result is `conflict` (AWS 409). A supplied version, on update or delete, must be a JSON integer of at least 1; anything else, including a boolean, a string or a non-integer number, is `invalid` and checked before `not-found` or `conflict`.
- **Limits.** Depth may not exceed 8 in either section. The section object is level 1, and every nested object or array adds a level, including objects inside arrays and arrays inside arrays; this array rule is the proposed SuiFleet convention. A rejected request leaves the document and version unchanged. `clientToken` may not exceed 64 bytes, and the serialized state may not exceed 8 KiB (`too-large`). Shadow names are 1–64 characters of `[A-Za-z0-9:_-]`, and `""` is the classic shadow.
- **Proposed differences from AWS.**
  - Deleting a shadow increments the version, and a recreated shadow continues the sequence. There is no 48-hour reset.
  - Cloud synchronization is a selected adapter with an explicit conflict policy (SF-033: a device-signed HTTP(S) adapter; AWS IoT Device Shadow sync is SF-061).

### Streams (SF-034)

Implemented for local streams and HTTP(S) export: see [STREAMS.md](STREAMS.md). The rest of this section is the specification it follows.

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

This runs `python3 scripts/greengrass_compat.py`, the checker's negative tests, and `scripts/test_greengrass_compat.cjs`. That script executes the recipe and managed-dependency fixtures against the real translator and checks every translation with the frontend recipe validator. The checker also validates the SF-038 [managed-component catalog](GREENGRASS_MANAGED_COMPONENTS.md). Two further checks run elsewhere:

- `make edge-test` runs the daemon's recipe and configuration validators on every translation.
- `make test-ota` runs `scripts/test_greengrass_runtime.py`, which translates an AWS YAML recipe with the CLI and runs it on the real daemon against the local simulator. It checks the run directory and `{work:path}`, the artifact path, the delivered default configuration, absence of the nucleus environment, the install directory, shutdown, a merge update, an upgrade, and the updater hazard behind `no-lifecycle-command`.

The runtime receipt is [`docs/validation/sf037-recipe-translation.json`](validation/sf037-recipe-translation.json).

| Suite | Cases evaluated by the reference model | Current-API execution | Runtime execution |
|---|---|---|---|
| `aws-recipe-cases.json` | 83 recipes (18 JSON/YAML texts) | Daemon and frontend validators (passing) | Passed: SF-037 translator, daemon validators and simulator run |
| `pubsub-cases.json` | 53 filter/topic/match/ACL checks | Daemon pub/sub module (passing) | Passed: SF-032 daemon, IPC UIDs and Mosquitto ([receipt](validation/sf032-messaging.json)) |
| `shadow-cases.json` | 55 steps across 10 sequences | Daemon shadow store, each step also after a reopen (passing) | Passed: SF-033 daemon, IPC UIDs, restarts and a verifying sync service ([receipt](validation/sf033-shadows.json)) |
| `stream-cases.json` | 46 steps across 5 sequences | Daemon stream store (passing) | Passed: SF-034 daemon, IPC UIDs, crashes and export ([receipt](validation/sf034-streams.json)) |
| `client-device-cases.json` | 16 authorization requests | none | Pending SF-032, SF-035 |
| `managed-dependency-cases.json` | 47 managed-dependency classifications | none | Passed: SF-037 translator classification |
| `credential-provider-cases.json` | 65 steps across 7 sequences | none | Passed: SF-057 local adapter ([receipt](validation/sf057-credentials.json)) |
| `cloudwatch-metrics-cases.json` | 90 steps across 8 sequences | none | Passed: local adapter/HTTP/SQLite ([receipt](validation/sf059-metrics.json)); AWS delivery pending |
| `cloudwatch-logs-cases.json` | 69 steps across 8 sequences | none | Passed: SF-060 adapter and local emulator ([receipt](validation/sf060-cloudwatch-logs.json)) |

`scripts/greengrass_compat.py` contains small **specification reference models**. They make each expected value checkable and keep the fixtures internally consistent. They are not the daemon, a translator or an adapter, and agreement with them proves nothing about runtime conformance. Each owning ticket must execute the same fixture files against its real implementation. That ticket then sets the suite to `passed` with a committed runtime receipt, and only after that may it raise any `aws_*` status. Fixtures may be corrected in review if they contradict AWS behaviour or an approved design. Correct them deliberately; never loosen them to make an implementation pass.

## Recommendations for Codex

These are recommendations only. Codex decides on approval and on any ticket changes.

1. **SF-037 (implemented).** Bootstrap, startup, recover and uninstall need a separate native-lifecycle decision before they can be translated. Per-step timeouts and environment need native recipe fields. `{configuration:/pointer}` interpolation needs a native recipe feature.
2. **SF-032/033/034/035.** Use the matching fixture files as required runtime acceptance, plus restart, disk-full, concurrency and authorization-failure tests on real processes.
3. **`aws_api` levels** are `undecided`. Recommendations:
   - Exclude AWS control-plane API emulation.
   - AWS IPC wire/SDK compatibility is a separate decision now that the native SF-027 API exists; see [IPC and SDK compatibility boundaries](#ipc-and-sdk-compatibility-boundaries).
   - AWS cloud targets (IoT Core bridge, shadow sync, Kinesis/S3 export, token exchange, CloudWatch) are SF-038 adapters with implementation tickets SF-057 to SF-063.
4. **SF-038.** Maps AWS-managed components, Lambda and Windows/macOS/x86 in [GREENGRASS_MANAGED_COMPONENTS.md](GREENGRASS_MANAGED_COMPONENTS.md). Lambda, industrial, ML example and non-initial platform gaps are SF-064 roadmap work, not approved exclusions.
