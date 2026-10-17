# Greengrass OTA and fleet-management parity audit

Audited 2026-10-13 against this repository and current AWS Greengrass V2 documentation.
Scope: functional equivalence for software/firmware OTA and fleet operations. This is not a claim of AWS API, recipe-format, SDK, IAM, or managed-component compatibility. Those require separate adapters and acceptance suites.

## Platform responsibility clarification

The platform installer owns OS installation, partitions, reboot, boot confirmation and firmware rollback. Greengrass-style component deployment requires verified artifact delivery and configured lifecycle execution, not automatic updater detection or a SuiFleet-owned OS recovery mechanism. Historical missing-feature rows below do not override this boundary; current tickets and implementation evidence govern completion.

## Conclusion

SuiFleet has a testable one-shot OTA foundation. It does not yet provide Greengrass-compatible fleet management or a complete component runtime. Local test coverage establishes simulator behavior, not public-network availability, production readiness, or successful hardware firmware updates.

The immediate platform blocker is transport. The daemon now uses the supported gRPC v2 API (SF-004: `ListEvents` discovery with durable cursors, locally built and verified transactions, effects-checked execution); the app migrated in SF-003; the publication resolver migrated in SF-006. Sui reports JSON-RPC retirement and recommends gRPC/GraphQL; WebSocket event subscriptions were deprecated earlier. Replacing WebSockets with durable HTTP polling repairs recovery on a compatible endpoint, but does **not** solve JSON-RPC retirement. A supported transport and transaction-builder migration is required before a public-network release. See [Sui gRPC status](https://www.docs.sui.io/develop/accessing-data/grpc/what-is-grpc), [migration cookbook](https://www.docs.sui.io/develop/accessing-data/grpc/grpc-migration-cookbook), and [TypeScript migration](https://sdk.mystenlabs.com/sui/migrations/sui-2.0/json-rpc-migration).

## Findings repaired in this change

- Contract event fields and recipe indirection were incompatible with the daemon. It now reads the recipe, validates identity/schema/hash, downloads the referenced artifact, and checks the on-chain SHA-256 before applying.
- The old address derivation used SHA3 rather than Blake2b-256, and transaction signing signed raw intent bytes rather than their Blake2b-256 digest. Both match an independent Mysten SDK vector now. See [Sui authentication](https://www.docs.sui.io/develop/transactions/transaction-auth/auth-overview).
- Recipe bytes now have their own on-chain SHA-256 dynamic field, created through the additive `create_deployment_verified` API. The daemon rejects missing or mismatching recipe digests; legacy creation remains callable for upgrade compatibility.
- Status calls passed a removed Clock argument and considered a transaction digest sufficient for success. Arguments now match the contracts and failed execution effects remain queued.
- Registration omitted capability issuance. The app now registers and issues the capability atomically.
- Direct-device deployments were blocked by the browser's group-only guard and ignored by the daemon. Both are fixed.
- Any deployment creator could trigger a matching group's daemon. The daemon now verifies the current Device owner, registered signing address, group owner and live membership before downloading/applying.
- Public capability issuance/transfer also allowed an operator to retain a cap and impersonate the device. New caps bind the registered signer address in a dynamic field, and reports require that signer; legacy unbound caps must be reissued.
- Any valid capability could report on unrelated deployments. Direct reports now require a targeted device; group reports require a targeted, creator-owned group containing that device.
- WebSocket disconnects and restarts lost jobs; report failures disappeared. An atomic, synced, locked journal now retains the event cursor and terminal outcomes and retries reports after failures/restarts.
- An interrupted updater could be repeated after restart. Interrupted work is recorded as failed with an inspection message; it is never automatically re-executed. This is conservative recovery, **not** a hardware rollback or exactly-once execution guarantee.
- Fresh enrollment could replay every historical firmware event. Enrollment now establishes a baseline; explicit `replay_history` is available when replay is safe. Existing cursors always catch up.
- Artifact downloads buffered whole firmware in memory. They now stream, with a 1 GiB limit; recipes have a 1 MiB limit. HTTP and apply timeouts are bounded.
- The deployments page did not load/display per-device status. It now reads the live dynamic status field, expands current group members, and omits deleted records. Queries paginate instead of truncating the fleet at 50 records.
- Provisioning/address CLI commands were documented but absent. Software provisioning is now idempotent, creates a 0600 key, and runs before daemon startup under systemd.
- The UI advertised run/shutdown that no daemon executed. SF-014 added the supervisor; the run/readiness/shutdown/restart controls are enabled again now that the behavior is tested. Install and run commands both require device-side opt-in. SF-015 replaced the explicit configuration rejection with recipe 1.2 configuration management.
- Tests had a stale function signature and invalid shared-object test timing. Those are corrected. Frontend SDK version skew is pinned to the wallet library's compatible version.

## Tracking coverage

See [the 51-capability ticket map](GREENGRASS_TICKET_COVERAGE.md) for current ownership scope. The expanded backlog separates Raspberry Pi build/deploy and live acceptance, messaging, shadows, streams, client-device auth, containers, recipe compatibility, managed-adapter specification and signed releases. A mapping is not proof of implementation or complete AWS compatibility.

## Feature matrix

“Partial” means a meaningful subset exists but does not satisfy the full AWS behavior. “Missing” means no implementation was found. Priorities: P0 blocks safe/public-network operation; P1 is required for the requested OTA/fleet product; P2 is required for broader Greengrass equivalence.

### Deployment and OTA

AWS provides device/group targets, component dependency resolution, deployment revisions, configuration updates, and policy-controlled rollout. See [deployment behavior](https://docs.aws.amazon.com/greengrass/v2/developerguide/manage-deployments.html) and [deployment creation/policies](https://docs.aws.amazon.com/greengrass/v2/developerguide/create-deployments.html).

| Capability | SuiFleet after fixes | Remaining requirement | Priority |
|---|---|---|---|
| Individual and static group targeting | Implemented, locally tested | Public-network/hardware acceptance | P0 |
| Artifact distribution and integrity | Partial: Walrus download + on-chain recipe/artifact digests | Storage renewal, confidential artifacts and live network validation | P0 |
| Durable offline delivery | Partial: cursor catch-up for an enrolled device; group desired state converges on the latest revision (SF-013) | Supported transport; retention/indexer guarantees | P0 |
| Trusted update authorization | Partial: current operator/group checks | On-chain creation authorization, key rotation/revocation and delegated roles | P0 |
| Deployment statuses | Partial: pending/applied/failed | Execution progress, explicit component/platform-reported outcomes, reasons and timestamps; do not infer OS boot health | P1 |
| Desired fleet state and revisions | Implemented for groups, locally tested (SF-013): revisioned component → deployment map per group; supersession; cross-group conflicts left as installed | Device-level desired state (after SF-008 custody); public-network/hardware acceptance | P1 |
| Newly joined devices receive desired state | Implemented, locally tested (SF-013): membership from DeviceAddedToGroup events, verified against the group object each poll | Event retention for long-lived fleets; public-network acceptance | P1 |
| Multi-component atomic deployments | Partial (SF-013): one revision commits several components atomically on chain; devices run an ordered, journaled plan that stops at the first failed step; each step installs its dependencies first (SF-012) | Ordering across desired components by dependency (steps run in name order), device-side rollback and lifecycle supervision (SF-014) | P1 |
| Staged/canary rollout | Missing | Cohorts, concurrency/rate limits, bake period and release gates | P1 |
| Failure-threshold abort | Missing | Fleet aggregation, minimum sample size and threshold evaluation | P1 |
| Pause/resume/cancel | Missing | Durable state transitions; deletion is not cancellation | P1 |
| Device update timeout | Partial: bounded command/download | End-to-end deadline, offline expiry, retry policy and backoff | P1 |
| Maintenance windows/update deferral | Missing | Scheduler and component safe-to-update handshake | P1 |
| Automatic application rollback | Missing | Versioned cache, reversible install, health checks and last-known-good state | P1 |
| OS/firmware recovery | External apply hook only | Verified handoff to configured platform installer scripts; preserve SuiFleet state and report explicit results. Platform owns installation, reboot and firmware recovery | P1 |
| Safe daemon self-update | Partial (SF-026): configured handoff to a verified, preflighted daemon binary through the base `--launch` binary; startup failures and hangs fall back via systemd restart/start timeout without the new daemon; real-systemd and simulator tested ([DAEMON_UPDATE.md](DAEMON_UPDATE.md)) | Physical power-loss during handoff and signed-release (SF-039) verification of daemon artifacts | P1 |
| Artifact lifecycle | Partial: short-lived Walrus uploads | Retention/renewal ownership, cache/GC, resumable downloads and disk quotas | P1 |

### Component runtime

AWS components define recipes, lifecycle, dependencies and platform selection. See [component development](https://docs.aws.amazon.com/greengrass/v2/developerguide/develop-greengrass-components.html), [recipe reference](https://docs.aws.amazon.com/greengrass/v2/developerguide/component-recipe-reference.html), and [component model](https://docs.aws.amazon.com/greengrass/v2/developerguide/greengrass-components.html).

| Capability | SuiFleet after fixes | Remaining requirement | Priority |
|---|---|---|---|
| Component versions/catalog | Partial: name/version strings in jobs | Immutable version registry, semantic versions, discover/resolve candidates | P1 |
| Recipe format | Partial: proprietary v1 JSON | Defined compatibility policy; AWS JSON/YAML adapter if format compatibility is desired | P1 |
| Install lifecycle | Partial: one-shot opt-in shell script or apply hook; supervised components install into per-version workspaces (SF-014) | Validation and install receipts | P1 |
| Run/startup/shutdown/bootstrap/recover | Partial (SF-014): supervised run with readiness, restart policy and budget, graceful shutdown, rollback of an unready replacement, dependency-ordered restart of committed versions on daemon start and after a daemon crash, no unmanaged descendants; simulator-tested | Configured component bootstrap, dependency failure coupling, health reporting on chain and physical validation of supervision restoration after platform-controlled restart; platform scripts own system reboot | P1 |
| Dependency resolution | Partial: recipe 1.1 SemVer requirements, hard/soft dependencies, deterministic dependency-first installs, and cycle/conflict rejection before changes ([SF-012](COMPONENT_DEPENDENCIES.md)) | Dependency lifecycle state coupling, rollback, and public-network/hardware acceptance | P1 |
| Platform manifests | Missing | OS/arch/platform selectors and multiple artifacts | P1 |
| Configuration merge/reset/validation | Partial (SF-015): recipe 1.2 defaults, JSON-pointer reset then recursive merge, persisted per component with rollback, validate-command accept/defer/reject before apply, device-local confidential overlay; locally tested | Per-deployment updates for catalog versions (on-chain field), in-place reload notification over IPC (SF-027), public-network/hardware acceptance | P1 |
| Runtime isolation | Partial (SF-053): device key and PIN held by a separate signing-service user with an intent-checked, caller-checked socket; component processes cannot read or use it ([KEY_ISOLATION.md](KEY_ISOLATION.md)); components still share the daemon user | Per-component users, environment, resource limits and IPC ACLs | P0 |
| Local deployment/component CLI | Partial: provisioning/address CLI | Install/list/start/stop/restart/status/logs and offline local recipes | P2 |
| Container component adapters | External command possible | OCI pull/auth, digest pinning, supervision and recovery | P2 |
| Lambda/AWS-managed component compatibility | Missing | Explicit adapters; cannot claim native compatibility | P2 |

### Fleet control, operations and identity

AWS includes device/component health, operational telemetry, logs and provisioning. See [monitoring](https://docs.aws.amazon.com/greengrass/v2/developerguide/logging-and-monitoring.html), [logs](https://docs.aws.amazon.com/greengrass/v2/developerguide/monitor-logs.html), and [fleet provisioning](https://docs.aws.amazon.com/greengrass/v2/developerguide/fleet-provisioning.html).

| Capability | SuiFleet after fixes | Remaining requirement | Priority |
|---|---|---|---|
| Inventory and groups | Partial: registration and manual groups | Installed versions, tags, search, filters, dynamic groups and ownership transfer semantics | P1 |
| Software key provisioning | Implemented, locally tested (SF-021): claim-sponsored self-enrollment (`suifleet --enroll`), operator ownership via DeviceOwnerCap, bulk claims funded in one PTB, quota/expiry/revocation | Image/first-boot integration (SF-025/029), public-network acceptance | P1 |
| Hardware identity/TPM/HSM | Partial (SF-021): signer abstraction; PKCS#11 P-256 (Sui secp256r1) backend tested with SoftHSM2 | Real TPM/HSM validation, ed25519 tokens, attestation | P1 |
| Key rotation/revocation | Missing | Revocable capability tied to active identity, migration and recovery | P0 |
| Online/last-seen/heartbeat | Partial (SF-019): signed heartbeat snapshots, bounded offline spool, indexer ingress checked against the registry identity, online/offline/last-seen in the API and dApp; locally and localnet-tested ([TELEMETRY.md](TELEMETRY.md)) | Hosted ingress, alerts, public-network and hardware acceptance | P1 |
| Installed component and device health | Partial (SF-019): installed versions, supervised phases and sync health per device; healthy/degraded/offline/unreported status, fleet summary API, dashboard independently verifying each report | Component-defined health checks and metrics, alerts, hosted service | P1 |
| Diagnostics and remote logs | Partial (SF-020): bounded rotation and rotation-safe shipping of daemon and component logs, on-device redaction (secrets, keys, phrases, confidential configuration), signed batches with offline spool, support-scoped audited search and diagnostic bundles, actionable alerts with webhook; locally and localnet-tested ([LOGS.md](LOGS.md)) | Hosted service, dApp log search, per-component log levels, hardware acceptance | P1 |
| Fleet API/CLI and automation | Partial (SF-024): versioned `/v1` API with filters/keyset pagination, unsigned bulk-operation transactions, long-poll change feed and `ctl` CLI | Webhook push delivery, CI integration examples, more bulk operations | P1 |
| Operator RBAC and delegation | Partial (SF-021): revocable group-scoped deployer roles with on-chain delegate audit | Further roles (enrollment/lifecycle managers), multisig/change approvals | P1 |
| Production indexer | Implemented, locally tested (SF-024): `suifleet-indexer` materializes events/objects into SQLite with durable cursors; versioned API, change feed, CLI | Hosted deployment, dApp migration onto the API, archival-node retention on public networks | P1 |
| Secrets and confidential configuration | Missing | Encrypted distribution, policy-controlled local cache and rotation; never plaintext on public chain/Walrus | P1 |
| Cost/network policy | Missing | Gas funding/sponsorship, storage budgets, renewal alerts and network quotas | P1 |
| Packaging/provisioning lifecycle | Partial: systemd and OE recipe | Pinned source, Yocto cargo dependency support and actual image/device test | P1 |
| Publish/upgrade tooling | Partial: development script | Scope metadata to active TOML environment and stop masking publish/parse failures; validate upgrades against a deployed package | P0 |
| Production release pipeline | Missing | CI, reproducible builds, signing/SBOM, compatibility/migration checks and release acceptance | P1 |

### Broader Greengrass capabilities

These belong to full Greengrass equivalence beyond OTA/fleet management. See [AWS-provided components](https://docs.aws.amazon.com/greengrass/v2/developerguide/public-components.html), [configuration IPC](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-component-configuration.html), and [local component-management IPC](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-local-deployments-components.html).

The [compatibility specification](GREENGRASS_COMPATIBILITY.md) (SF-028) separates functional, AWS-recipe and AWS-API compatibility for these rows and provides proposed contract fixtures; it does not implement any of them.

| Capability | Status | Required direction | Priority |
|---|---|---|---|
| Component IPC and SDK | Native v1: Unix UID ACLs, Python SDK/CLI, authenticated deployment install and supervised management, public config reads, health and volatile messaging ([COMPONENT_IPC.md](COMPONENT_IPC.md)) | Per-component execution users for shared-user supervised components; AWS wire compatibility and durable messaging remain separate | P2 |
| Local pub/sub and MQTT bridge | Missing | Broker/bridge adapters, permissions and offline delivery | P2 |
| Device shadows | Missing | Desired/reported state with version/conflict semantics and local sync | P2 |
| Stream manager/store-and-forward | Missing | Bounded durable streams, export adapters and recovery | P2 |
| Client-device discovery/authentication | Missing | Local device discovery, credentials, certificates and gateway authorization | P2 |
| Token exchange/cloud credentials | Missing | Scoped credential provider or equivalent decentralized service credentials | P2 |
| ML inference/industrial/cloud integrations | Missing | Optional component ecosystem/adapters and compatibility tests | P2 |
| Windows/macOS runtime | Missing: Linux-oriented | Explicit OS support matrix and implementation/testing | P2 |

## Release gates and sequencing

1. **Supported network and trust boundary (P0):** migrate app wallet/client and edge reader/builder/executor to supported Sui APIs; separate type origins from call package IDs; independently verify authenticated recipes across the supported transport; add revocation and runtime privilege boundaries. Verify chain identity, transaction effects and gas recovery. Run real testnet registration and successful/failed reports.
2. **OTA engine (P1):** introduce immutable component versions and a deployment manifest/revision model; reconcile desired state; resolve dependencies/platforms; install into versioned directories; supervise components; add explicitly configured application recovery and platform-installer handoff. Test SuiFleet execution/journal recovery across restarts; validate platform-controlled firmware recovery separately on the selected hardware.
3. **Fleet scheduler (P1):** add explicit pause/cancel/expiry, staged cohorts, concurrency and abort gates. A coordinator/indexer should compute rollout eligibility; devices must verify their authorization and eligibility. Test offline cohort members and membership changes.
4. **Fleet operations (P1):** add health telemetry, installed inventory, logs, secrets, onboarding/revocation, roles, fleet API/CLI, alerts, gas/storage funding and renewal. Use off-chain services for high-volume/private data; chain stores authority and compact deployment receipts.
5. **Extended compatibility (P2):** publish a component SDK and adapters for messaging, shadows, streams, client devices and selected AWS recipes/components. Run a capability-by-capability acceptance suite before claiming Greengrass equivalence.

For each feature, completion requires contract/API semantics, daemon behavior, operator controls, migration/recovery handling, and negative-path integration coverage. A checkbox or a recipe field alone is not implementation.

## Known limits of this repaired baseline

- Newly introduced dynamic-field key types originate in the package version that introduced them. Current clients assume all key origins equal the first package ID, so upgraded deployments need a per-type origin resolver.
- Older package versions remain callable and can bypass added authority checks on shared state. Tests against only the latest code do not prove upgrade security; authoritative new state must be protected from old entry points.

- The legacy transport remains a public-network release blocker; local simulator tests cannot resolve that.
- `contracts/Published.toml` is empty. Separate ephemeral devnet metadata exists in `Pub.devnet.toml`, but no current live deployment was verified. Nothing was published or hardware-programmed during this change.
- Ownership and membership checks use a trusted RPC response and can race changes before apply. The on-chain group report checks current membership, which can reject a report after removal. Such reports remain queued and need lifecycle/terminal error policy.
- Deregistering a device stops this daemon from accepting new work because the Device lookup fails, but existing transferable caps are not fully revoked on-chain.
- Deployments not referenced by a group's desired state are still one-shot work: enrolled offline devices replay them in order. Desired state (SF-013) coalesces superseded versions and reaches newly joined members, for groups only; component removal retires management but does not uninstall (SF-014).
- Status retry is durable but has no terminal dead-letter/backoff policy; deleted records, revoked caps or exhausted gas need operator resolution.
- A crash between successful hardware apply and journal completion is ambiguous. Recovery reports failure and requests inspection; automatic rollback is absent.
- Apply success means command exit status, not firmware boot health. Interrupted or timed-out commands can leave partial changes; the updater must provide its own atomicity/recovery.
- Legacy deployment records without a recipe digest are intentionally rejected. The daemon now verifies an on-chain recipe SHA-256 before executing lifecycle text; RPC integrity remains a trust boundary.
- Browser event scans now paginate but are not an efficient fleet index; deployment group expansion reflects current membership rather than a historical target snapshot.
- TPM support and the original README's `--features tpm` were aspirational, not implemented. Existing devices registered using the former SHA3 address need re-enrollment under the corrected address.
- The development publish script still uses unscoped metadata greps and suppresses errors in its publish/pretty-print pipeline; it needs hardening before automated releases.
- Historical baseline limitation: Yocto cross-compilation and platform-installer physical recovery were unvalidated at audit time; consult current build and integration evidence.

## Validation evidence

See [validation results and limits](VALIDATION.md) for the exact checks run, tested failure paths and enrollment/upgrade requirements.
