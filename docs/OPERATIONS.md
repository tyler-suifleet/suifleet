# SuiFleet operations and verification

This runbook describes current local operations and the intended release gates. It does not establish production readiness. Current release blockers include incorrect origins for newly introduced key types, and old-package entry points that can bypass new checks on shared authoritative state.

## Environment

- Host: Linux/WSL, Git, Docker Engine/Compose and GNU Make.
- Container: Ubuntu 22.04, pinned Sui CLI and Rust, Node 22, pnpm and Python 3. Read exact build arguments in `Dockerfile`.
- `make dev` builds/starts `dev`; `make shell` opens it. Source is bind-mounted at `/workspace` under a host-UID/GID-matched user.
- Browser frontend: `http://localhost:3000` after `make dapp-dev`.
- Cache/wallet volumes: `cargo-registry`, `cargo-git`, `pnpm-store`, `move-cache`, `sui-config`.
- Device service: `suifleet.service`, config/key in `/etc/suifleet`, journal under `/var/lib/suifleet`; service provisioning is idempotent.
- Sui authorizes deployments/reports, Walrus holds recipes/artifacts, and the device invokes an operator-configured updater. Devices can report signed heartbeat and health telemetry to the fleet indexer ([TELEMETRY.md](TELEMETRY.md)). No production indexer, coordinator, hosted health ingress, secrets service or hosting deployment is currently recorded.

Fleet wallet and native multisig handover uses the recipient-confirmed procedure in
[FLEET_HANDOVER.md](FLEET_HANDOVER.md). Transfer all relevant authority records and
explicitly supersede catalog/decryption/provider authority; transferring a device
capability alone is not complete fleet administration migration.

## Reproducible local checks

From the host:

```bash
make dev
make dapp-install
make check
```

`make check` runs Move tests, the contract-interface drift check, Rust tests, the actual-daemon OTA simulator, frontend typecheck and static production build. It never publishes contracts or flashes hardware.

`make check-interfaces` checks the reviewed [shared interface catalog](SHARED_INTERFACES.md) against actual compiled Move signatures and ordered layouts, verifies generated TypeScript/Rust files, and runs negative ABI changes, cross-client BCS vectors and shared recipe compatibility cases. `make generate-interfaces` regenerates clients only when the compiled ABI matches the reviewed catalog; intentional ABI changes require explicit capture and review. The original name/arity scanner remains an additional guard for references outside generated helpers, with its offline regressions in `make test-interfaces`. Version/status/type-origin metadata is shared by both clients and the manifest resolver; unknown protocols/recipe versions fail before apply.

Inside `make shell`, the equivalent commands are:

```bash
sui move test --path contracts
cargo test --locked --manifest-path suifleet-edge/Cargo.toml
cargo build --locked --manifest-path suifleet-edge/Cargo.toml
python3 scripts/test_ota.py
(cd dapp && pnpm install --frozen-lockfile && pnpm typecheck && pnpm build)
git diff --check
```

For release artifacts:

```bash
sui move build --path contracts
cargo build --locked --release --manifest-path suifleet-edge/Cargo.toml
(cd dapp && pnpm build)
```

The daemon executable is `suifleet-edge/target/release/suifleet`; the Next static export is `dapp/out`. They are build artifacts, not deployed releases.

The simulator is stdlib Python. It uses temporary keys, local HTTP servers and a harmless updater; it never spends gas or programs hardware. It validates recipe/artifact digests, direct/live-group targeting, bad hashes, unsupported lifecycle, untrusted operators, deleted objects, pagination, enrollment baseline, offline catch-up, restart deduplication and failed status retry. It also checks identity/signature against an independent Mysten SDK vector.

`make test-component` (also run by `make check` and CI) covers the harmless versioned test component in [tests/components/suifleet-test-app](../tests/components/suifleet-test-app/README.md). Its fixture tests run the real shell scripts, under a busybox-only `PATH` too when busybox is installed. It then runs the real daemon against the same simulator with `suifleet-test-updater` as `apply.command`, for v1, v2, an injected install failure and an injected health failure. It checks statuses, reports and the device receipts' version, run ID and artifact SHA-256.

The historical baseline has 13 passing Move tests and 10 Rust tests; counts can legitimately change. Verify results for the actual commit rather than asserting those numbers as a release requirement. Simulator mocks are not proof of network transaction building, wallet compatibility, package upgrades or post-reboot health.

If Rust files change, run `cargo fmt --manifest-path suifleet-edge/Cargo.toml` with rustfmt installed, then rerun relevant tests. The minimal container toolchain may need `rustup component add rustfmt` first. Do not silently upgrade the toolchain to make failures disappear.

## Continuous integration

Move-specific compiler/linter controls, the source audit, conditional formal
proofs and exact native/framework modeling limits are recorded in
[Move security](MOVE_SECURITY.md). Compiler success is not a security proof.

`.github/workflows/ci.yml` runs on every pull request and push to `main`. It builds the dev image from the pinned Dockerfile and runs `make ci` inside it: frozen pnpm install, Move tests, drift-gate regression tests, interface drift check, Rust tests, the OTA simulator, the test-component checks, typecheck, static build and `git diff --check`. Each step's output and `toolchain-versions.txt` are uploaded as the `ci-logs` artifact (14 days). The job has a read-only token and no secrets; it never publishes, spends gas or touches hardware.

All toolchain inputs are Dockerfile build arguments: base image digest, Sui release and SHA-256, Rust, Node package version and pnpm. Bump them deliberately, in their own commit, together with lockfile and compatibility-test updates; CI then runs against the new image. Workflow actions are pinned to commit SHAs.

Regressions these gates have been shown to catch: renamed/re-shaped Move functions and types used by clients (interface check), treating failed execution effects as success (Rust test and simulator), and reapplying journaled deployments after a crash before the cursor is saved (simulator).

Keep these gates deterministic rather than retried (SF-108):
- Rust unit tests share one process with the supervisor tests' orphan sweep, which kills and reaps every unregistered child. Spawn test children through `supervisor::spawn_std_tracked` and release them with `forget_std_tracked`.
- Wait for a condition or an exit, not a guessed sleep. A timing assertion may only become *more* true with delay, as with sleeping past a deadline.
- Read a status file once per condition. Scan a daemon's state only after stopping it, because it replaces files through temporary renames.
- Have simulators publish a chain change at the exact read that exposes a window, instead of racing the daemon's poll.

### Live gates

Public Sui testnet is normal automated validation under the user's standing authorization. `.github/workflows/testnet.yml` runs after successful ordinary CI on trusted main pushes and authenticated same-repository PR heads, daily, and on main-only dispatch. The dedicated `live-package-upgrade` environment is main-only, has no human reviewers, and cannot be bypassed by administrators. It holds only the dedicated testnet wallet. The runner verifies testnet chain `4c78adac`, the wallet, UpgradeCap, per-type origins and execution effects. See [TESTNET.md](TESTNET.md) for spending, encrypted cache and exact-head status rules.

`.github/workflows/live-gates.yml` delegates manual `package-upgrade` requests to the same automatic controller (main/testnet only, no arbitrary ref). All paths share concurrency `suifleet-testnet-ci-wallet` with cancellation disabled. Wallet and hardware environments retain required human approvals; their suites remain SF-010/SF-031 work. Mainnet, production credentials/services, physical Pi programming and unrelated wallets remain outside the testnet grant.

The sole PR orchestrator requires `testnet/package-upgrade` success bound to the current head and base once the controller exists on the integration base. Bootstrap is limited to the first reviewed workflow merge; missing or pending status blocks subsequent merges. Install the reviewed coordinator after integration, retaining its private configuration. A green offline CI result alone cannot satisfy this gate.

## Tests required beyond the local suite

| Boundary | Acceptance evidence |
|---|---|
| Fresh chain deployment | Actual registration/cap issuance, group/direct deployment, device report and observed UI statuses |
| Wallet | Connect/disconnect, chain mismatch, account changes, transaction rejection and failed execution effects |
| Package upgrade | Deploy v1, create objects, upgrade, preserve state, resolve each type origin and exercise both old/new clients |
| Security after upgrade | Attempt old-package calls and prove they cannot alter authoritative new receipts/authorization state |
| Storage | Actual Walrus bytes/digests, unavailable/expired blobs, renewal and configured provider behavior |
| Offline/runtime recovery | Disconnects, report service/gas failures, restart mid-download/mid-apply and durable recovery |
| Firmware adapter | Real hardware, reboot/power loss, health confirmation and rollback to known-good firmware |
| Yocto | Actual supported BitBake build, source pinning, cargo dependencies, installed service and first boot |

Report unavailable credentials/hardware as unverified evidence. Do not erase a journal, reenroll devices or generate chain transactions merely to get a local test green.

### Old package entry points and status receipts

Every published package version stays callable after an upgrade, and code can only access types its own package defines. The original package's `report_status` has no signer or target check. It writes the `StatusesKey` dynamic field and emits `DeviceStatusUpdated`, and both types originate in that package. Treat them as **unauthenticated legacy data**.

Authoritative status lives in state that old code cannot construct:

| | Legacy (forgeable) | Authoritative |
|---|---|---|
| Field | `StatusesKey → VecMap<ID, u8>` | `ReceiptsKey → Receipts { version, statuses }` |
| Event | `DeviceStatusUpdated` | `DeviceStatusReported { …, reporter }` |
| Type origin | First package | The upgrade that introduced receipts |
| Reader | `statuses()` | `receipt_statuses()`, `receipt_status()`, `has_receipts()` |

- New records carry no `StatusesKey`, so the old `report_status` aborts on them.
- Records created by the original package keep `StatusesKey` as unverified history. Old entry points can still write it. Their receipts are attached lazily on the first signer-checked report or by `migrate`, and legacy entries are never copied into receipts.
- `Receipts.version` gates writers. Any later upgrade that changes report authority (revocation, new targeting rules) must bump `RECEIPTS_VERSION` and then call the new package's permissionless, idempotent `migrate` on every live record. Once a record is migrated, older packages' report and `migrate` calls abort with `EPackageOutdated`. Records not yet swept remain writable by the previous package, under that package's own checks.

Mixed clients:
- The dApp reads `ReceiptsKey` through the deployment manifest's per-type origin (the upgrade that introduced receipts, not the original or call package). A record with no receipts is shown as **unverified**, never with its legacy statuses.
- Clients that still read `StatusesKey` are incompatible: new records have no such field, and on legacy records it is forgeable. Upgrade the dApp together with the contracts.
- Dynamic-field lookups by name must encode an empty key struct as `{"dummy_field": false}`; a real node rejects `{}` ("Missing field dummy_field").
- Since SF-008, `report_status`/`report_status_for_group` keep their signatures but always abort (`EUseVerifiedReport`). They cannot see the registry, so they cannot tell a revoked device from an active one. Devices report through `report_verified_status`/`report_verified_status_for_group`.

Recovery:
- Legacy unbound caps cannot report. Reissue them with `device_cap::issue_cap`.
- Devices registered before SF-008 have no registry identity and cannot report until their owner activates them with `device_registry::activate_device` (see below).
- Legacy records show no authoritative status until the device reports again. Redeploy if an operator needs a verified receipt.

Local acceptance (throwaway localnet in the dev image; refuses non-localhost RPC):

```bash
git archive <prior-ref> contracts | tar -x -C /tmp/v1
SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
SUI_CONFIG_DIR=/tmp/sui-config sui client switch --env local
SUI_CONFIG_DIR=/tmp/sui-config python3 scripts/upgrade_security_localnet.py --v1 /tmp/v1/contracts --v2 contracts
```

It publishes the prior package, creates devices and deployments (including a legacy record with only forged statuses), then upgrades. Next it attacks through old and new entry points, requiring each attack to abort at the intended check. It also verifies a later version-bumped upgrade locks out this package, then resolves a deployment manifest and runs the real dApp `fetchDeploymentEvents` against new, legacy and forged-only records. Results come from on-chain effects, object state and events. It needs `dapp/node_modules` (`make dapp-install`). This does not replace an upgrade test against an existing deployment on a public network.

### Device identity lifecycle (SF-008)

A device's caps are owned by the device key, so the operator cannot change them. Trust therefore lives in the shared registry, under types introduced by the SF-008 upgrade that older packages cannot touch:

- `CustodyKey { device_id }` is a dynamic object field on the registry holding the activated `Device`. Older packages' `deregister_device` needs the Device by value, so it can no longer delete a trusted device behind its identity.
- `DeviceOwnerCap { device_id }` goes to the operator on activation. Whoever owns it controls the device; transferring it transfers ownership.
- `IdentityKey { device_id } → Identity { signer, revoked, owner_cap }` records the trusted key. The daemon applies only deployments created by the owner of `owner_cap`.

A report is trusted only when the cap is bound to the sender **and** the Device is in custody **and** its identity is unrevoked with that sender as signer (`device_cap::assert_active`).

| Action | Call | Who | Effect |
|---|---|---|---|
| Register | `register_device` + `issue_cap` + `activate_device` in one PTB (the dApp does this) | Anyone | Device in custody, identity active, owner cap to the caller |
| Activate | `activate_device(registry, Device): DeviceOwnerCap` | Owner of an uncustodied Device | Required for devices registered before SF-008 (the dApp shows **Activate**); existing bound caps keep working |
| `&Device` calls | `borrow_device(registry, &cap)` → `issue_cap` / `device_group::add_device` / `set_meta` → `return_device` | Owner-cap holder | The `DeviceLoan` hot potato must be repaid with the same Device and key in the same transaction |
| Revoke / reinstate | `revoke_device` / `reinstate_device(registry, &cap)` | Owner-cap holder | No report is trusted until reinstated |
| Rotate key | `rotate_device_key(registry, &cap, new_address)`, then borrow and `issue_cap` | Owner-cap holder | Every cap bound to the old key is distrusted; a revocation stays in force |
| Deregister | `deregister_activated_device(registry, cap)` | Owner-cap holder | Deletes Device and owner cap, leaves a revoked identity tombstone |

A registered but unactivated Device (including one registered through an older dApp or package) is owned directly and can be deleted by any package version, but it is never trusted. The daemon checks its identity on every poll; while revoked, rotated away or not activated it applies nothing and logs why, and resumes once the identity is active again.

**Receipts from older authority versions.** `RECEIPTS_VERSION` is 2. The previous package still writes receipts at version 1 under its signer-only check, also for a revoked key, on records it created (including after the sweep) or that were never migrated. These are never trusted:
- `receipt_status`/`receipt_statuses` return only current-version receipts (`has_current_receipts`); the dApp shows version-1 receipts as unverified;
- `migrate` and the next current report discard version-1 statuses instead of promoting them;
- `DeviceStatusReported` events can also come from the previous package, so they are notifications, not evidence.

**Upgrade rollout:**
1. Upgrade the package. Old-signature reports abort from then on; devices keep their reports queued.
2. Regenerate the manifest (the new types have the upgrade's origin), the dApp and the daemon (`report_verified_status`, owner-cap operator lookup).
3. Have each owner activate every device registered before the upgrade. Until then the device is untrusted and the daemon applies nothing.
4. Sweep `deployment_manager::migrate` over every live deployment record. This locks out the previous package and discards statuses it wrote. The daemon does not re-report deployments it already reported, so those devices show no status until their next deployment; redeploy if a verified receipt is needed now.

**Known limits:**
- Older dApp builds register without activating; such devices stay untrusted until activated. A new owner-cap holder inherits the device's current key; rotate it if the previous owner may have a copy.

Local acceptance across a real upgrade (prior contracts, then this package):

```bash
git archive origin/main contracts | tar -x -C /tmp/prior      # contracts before SF-008
SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
SUI_CONFIG_DIR=/tmp/sui-config sui client switch --env local
SUI_CONFIG_DIR=/tmp/sui-config python3 scripts/identity_lifecycle_localnet.py --prior /tmp/prior/contracts --new contracts
```

It checks, from on-chain effects, exact abort sites or execution errors, and receipt state:
- the prior package cannot write records created or migrated by the upgrade, and the retired signature aborts;
- a pre-upgrade device is untrusted until activated, then reports with its existing cap;
- the prior package's `deregister_device` cannot take an activated Device, directly or borrowed (`InvalidLinkage`; a by-value consumer leaves the loan unpaid);
- revocation blocks reports and reinstatement restores them; rotation distrusts the old key; the owner cap carries lifecycle authority to a new owner; deregistration blocks reports;
- a revoked key's prior-package reports on unmigrated and post-sweep records stay version 1, are discarded by migration or the next current report, and the dApp's `readReceipts` reads them as unverified from the live chain.

`scripts/upgrade_security_localnet.py` covers the earlier SF-001 transition; run it with `--v2` set to contracts before SF-008.

## Daemon chain access (gRPC)

The daemon talks to the manifest's `rpc_url` over the Sui gRPC v2 API only (Mysten's Rust SDK `sui-rpc` 0.4). Walrus downloads still use plain HTTP.

**Node requirements:** the node must serve indexed `LedgerService.ListEvents`. Sui 1.71.1 returns `Unimplemented`; 1.81.1 localnet, testnet (1.82.1) and mainnet (1.81.1) full nodes serve it. The dev image pins Sui 1.81.1.

**Reads:**
- The chain identity comes from `GetServiceInfo`. The manifest's 8-hex `chain_identifier` is the first four bytes of the genesis digest.
- Package type origins are decoded from the package's own BCS.
- Objects are read with `GetObject`. Dynamic fields are read by their derived object ID; empty key structs serialize as one `false` byte.

**Discovery:**
- `ListEvents` is filtered by the manifest's `DeploymentCreated` type.
- The journal stores the opaque resume cursor, with its checkpoint, after every event and after every scanned page. Enrollment starts after the current checkpoint.

**Retention:**
- Full nodes prune history; `GetServiceInfo.lowest_available_checkpoint` reports the oldest retained checkpoint.
- If a device's resume position is older than that, polling stops with an explicit error and nothing is skipped. Point `rpc_url` at a node that retains the history (an archival or indexed provider), or re-enroll deliberately; re-enrollment skips everything before it.
- Plan node retention to exceed the longest expected device downtime.

**Cursor migration:** a journal written by the JSON-RPC daemon (`{txDigest, eventSeq}`) resumes strictly after that exact event. The daemon reads the transaction's checkpoint and walks the filtered event stream in ledger order (transaction, then event index) to the event itself, so earlier events in the checkpoint, even in the same transaction, are never redelivered. This matters because the journal only remembers deployments that were acted on: one skipped earlier could otherwise run if its targeting changed since. If the node no longer holds that transaction, or the event cannot be found in its checkpoint, polling stops with an explicit error instead of guessing.

**Status reports:**
- The node simulates the call to resolve object versions, gas and budget.
- Before signing, the daemon rejects anything but exactly one `deployment_manager::report_verified_status` or `report_verified_status_for_group` call. It must have the intended objects (record, registry, cap and, for groups, the group) and status, be sent and paid by the device key, and have a budget of at most 0.05 SUI.
- The device address therefore needs a small SUI balance for gas.
- Success is read from execution effects.

**Tests:**
- `make test-ota` and `make test-component` run the daemon against the Python simulator through `suifleet-edge/examples/grpc_sim_shim.rs`, which verifies signatures with Mysten's `sui-crypto`.
- `make test-daemon-localnet` runs it against a real disposable localnet (start `sui start --with-faucet --force-regenesis` first): direct, group, failure, offline catch-up and cursor-migration paths, judged from on-chain receipts.
- `make test-seal-localnet` runs confidential delivery against a disposable localnet (start it first; add `--upgrade-from REF` for the upgrade path): dapp encryption, three test Seal key servers, signing-service sessions and the daemon in `[seal] mode = "required"`, judged by on-chain receipts. `make live-seal-delivery SUIFLEET_WALLET=…` does the same on testnet with Mysten's key servers after `make live-gate-package-upgrade`. See [CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md).

## Desired state (SF-013)

A group can carry a **desired state**: a revisioned map from component name to the deployment its members should run (`desired_state` module, a `DesiredKey` field on the group).

**Changing it on chain:**
- Only the group admin can change it, one revision at a time. In a PTB: `begin`, then `set_component`/`remove_component`, then `commit`.
- A revision's changes commit together.
- `set_component` accepts only the admin's own, verified-recipe deployments that target the group, and marks them managed.
- The dApp's firmware page creates the deployment and commits it as the groups' desired version in one transaction, by default. Targeting individual devices stays a one-shot deployment.

**How the daemon reconciles each poll:**
- **Group discovery.** It finds its groups from `DeviceAddedToGroup` events, scanned from genesis with their own journal cursor.
- **Membership check.** It confirms membership, and that the group's admin is the device's operator, from each group object.
- **Authority re-check.** After reading every group's desired state it reads the operator again. If ownership changed in between, it plans nothing that poll, so a transfer cannot admit the previous operator's new desired state.
- **Merge.** It merges the groups' current revisions. Groups naming different deployments for one component are a conflict: the component is left as installed, and the dApp shows it.
- **Plan.** In name order, it compares each desired deployment with what the component currently runs: the deployment it last started for that component, one-shot or desired, recorded with that deployment's intent. It applies desired deployments that have never run here, journaling the step and then the intent before the updater starts.
  - The first step that does not apply stops the plan, and that desired state is not retried until it changes. The step is journaled with the desired state it was started for, and the stop is read back from the journal on every poll, so it holds across a crash. A newer desired state waits while the step's update is still in flight in the privileged helper. Once that update settles (Failed or indeterminate), the newer state is planned, even with no further change and across restarts.
  - A crash mid-step is settled Failed on restart and never re-executed. Later steps of the same desired state stay stopped.
  - A deployment never runs twice: its ID is also its journal entry, firmware request and receipt. If a revision selects a deployment again after the component has moved on (A → B → A, or a one-shot update after a desired one), the daemon logs that this is unsupported and leaves the component as installed, without recording it as converged. To return to a version, create a new deployment record for it.
- **Results.** Superseded deployments are never applied. New members receive the current state.
  - Members that leave, and components removed from a revision, are retired from management. They are not uninstalled: there are no lifecycle hooks yet (SF-014).
- **One-shot path.** It ignores records marked managed. Unmanaged deployments keep their one-shot behaviour.

**Limits:**
- There is no device-level desired state yet. It waits for SF-008's device custody; use a one-device group meanwhile.
- Membership discovery needs the node to retain `DeviceAddedToGroup` history. Pruned history stops reconciliation with an explicit error, as for deployment events.
- No public-network or hardware acceptance has been run.

**Tests:**
- Simulator: `scripts/test_desired_state.py` (in `make test-ota`).
- Real localnet across an upgrade of main's contracts: `make test-desired-state-localnet`.
- dApp builder executed on chain: `scripts/test_frontend_live.cjs`.

## Deployment process

1. Determine the explicit deployment scope and target network; verify the RPC's chain identity, signer address, gas and UpgradeCap ownership. Environment aliases alone are insufficient, especially after devnet resets.
2. Validate the release compatibility matrix and all relevant checks above. Resolve unsupported transport and upgrade-security blockers before a public-network release.
3. Prepare migrations for existing capabilities, recipes and state. Added dynamic fields do not appear automatically on old objects. New type keys can originate in the upgrade package rather than the initial package.
4. Publish an initial package or perform a compatible upgrade. Existing public signatures/layouts/abilities must remain compatible; an initializer is not a migration. Record execution effects, transaction digest, latest call ID, per-type origins, shared object IDs and UpgradeCap ID.
5. Generate frontend and device configuration from the network deployment manifest. `scripts/redeploy.sh` does this together with step 4 ([DEPLOYMENT_MANIFEST.md](DEPLOYMENT_MANIFEST.md)).
6. Rebuild the frontend after environment changes. `NEXT_PUBLIC_*` variables are compiled into the static export; editing server environment after build does not rewrite those assets.
7. Stage daemon rollout, verify enrolled identity/cap binding and preserve each device's journal. Do not force firmware replay by deleting state. Run a real end-to-end update/report and negative-path acceptance test before expanding the rollout.
8. Record artifacts, versions, IDs, migrations, results and recovery instructions in the release issue. Keep private credentials out of all artifacts.

The current development command is:

```bash
make publish-contracts PUBLISH_ARGS="--expect-address 0x... --dry-run"   # then without --dry-run
```

It runs `scripts/redeploy.sh` (`scripts/publish.py`). Without `--dry-run`, it spends gas and mutates the chain. It is not part of `make check`.

**Before any transaction, it verifies:**
- the live chain identity, against `Move.toml` and this environment's own `Published.toml` section;
- the signing account;
- for an upgrade, the UpgradeCap's owner, package and version.

**When something fails:**
- A failed command, failed effects or bad metadata stops it.
- If a later step fails after the transaction has executed, the transaction is kept in a pending record. Prior configuration is preserved.

**On success**, it records the release under `deployments/ENV/releases/`. The record holds all IDs and type origins, plus generated frontend and device configs. `deployments/ENV/current` and `dapp/.env.local` are then switched to it together, under a journal: if the process is killed mid-switch, the next run (or `scripts/redeploy.sh --reconcile`) finishes it, and `make dapp-build` refuses until then.

Details and recovery are in [DEPLOYMENT_MANIFEST.md](DEPLOYMENT_MANIFEST.md). `make test-publish` and `make test-publish-localnet` cover it. It has not yet been run against testnet.

The frontend reads only `NEXT_PUBLIC_DEPLOYMENT_MANIFEST`; the daemon reads the manifest through `[sui] manifest_path` (`suifleet-edge/config.example.toml`).

## Static frontend and Linux deployment

`pnpm build` produces the static `dapp/out` export. The [SF-082 development snapshot](SERVERLESS_FLEET_DATA.md) adds a checksum-pinned site-builder and deployment-bound reproducible build helper. No Walrus Site publication/update/rollback has been validated; its backend replacement is incomplete and must remain draft. Follow the recorded preflight/authorization and acceptance requirements before publication.

Install the daemon, systemd service and real config through the validated image/package workflow. The separate root first-boot provisioning service invokes `--provision`; the unprivileged daemon starts after enrollment config appears and creates its state directory. Register the printed address, record Device/DeviceCap IDs, and fund report gas. Apply success currently means updater exit code zero, not post-boot health.

The Yocto layer is `suifleet-edge/meta-suifleet`. [YOCTO_PACKAGING.md](YOCTO_PACKAGING.md) describes its pinned source/crate graph, Scarthgap Rust-mixin and Wrynose build profiles and separate first-boot provisioning/enrollment services. The source pin includes the merged SF-004 gRPC migration; offline packaging/boot acceptance remains distinct from supported-network OTA acceptance. Record actual build and boot results rather than inferring them from recipe presence. SWUpdate/RAUC installation and A/B recovery are platform-owned; SuiFleet invokes configured installer commands.

## Execution privileges

| Principal | Runs | Can write | Cannot |
|---|---|---|---|
| `suifleet` user (`suifleet.service`) | Daemon: chain polling, downloads, journal, recipe `install` scripts | `/var/lib/suifleet` only | Change its config or the helper config, use capabilities, create namespaces, see other processes, exceed 256 MiB / 64 tasks |
| root (`suifleet-firmware.service`, path-activated) | Only the command in `/etc/suifleet/firmware.toml` | Devices and system partitions, as the firmware updater needs; its own `/var/lib/suifleet-firmware` | Use the network, accept a command from the daemon |
| root (`suifleet-provision.service`) | First-boot key provisioning and handing the key to `suifleet` | `/etc/suifleet/device.key`, public address | Regenerate an existing corrupt key |

Every updater/recipe child gets a cleared environment (`PATH`, `LANG`, `HOME` only), its artifact directory as working directory, no stdin, `RLIMIT_CORE=0`, at most 1024 descriptors, optional `max_memory_bytes`, and the apply timeout. On timeout or cancellation the whole process group is killed. A spawn denied with EACCES, exit code 126 or 127, a signal, or any non-zero exit is an error, so the deployment reports Failed, never Applied.

With `apply.mode = "privileged-helper"`, firmware without a recipe `install` script goes through a spool in `/var/lib/suifleet/firmware`:
1. The daemon copies the verified artifact there and publishes a request containing its SHA-256.
2. The helper opens the spool only through `O_NOFOLLOW` directory descriptors.
3. It copies the artifact into its own root-only directory, re-hashes that copy, and runs the root-configured command on it.
4. It hands back a result file owned by the spool owner.

Claiming and withdrawal cannot both succeed, so a timeout never misreports an update:
- The helper claims a request by locking `<id>.json` and renaming it to `<id>.claimed`, and holds the lock until its result is written. The helper's own `timeout_seconds` bounds the updater.
- `apply.timeout_seconds` (plus 30 s for the path unit to start the helper) bounds only the wait for a claim. When it expires, the daemon withdraws by unlinking `<id>.json`. If that succeeds, the updater never ran and the deployment reports Failed. If the request was already claimed, the daemon keeps waiting while the helper holds the lock.
- If the helper dies mid-update (crash, power loss, reboot), the outcome is unknown. The deployment is marked indeterminate: it gets no status, is neither reported nor re-applied, and is never turned into Failed, including across restarts. The error is kept in the journal. The path unit also watches `*.claimed`, so the next helper run, at the latest at boot, turns the abandoned claim into an "interrupted" result without re-running the updater.
- Request IDs are derived from the deployment ID and journaled before the update starts. On every poll the daemon collects results for deployments still without a status, including updates that finished after it stopped waiting or restarted. A result is removed only after its outcome is journaled.
- On startup, a deployment the previous run left without a status is settled from the spool before anything else reads the journal. A written result or a claim keeps it pending for the collector. An unclaimed request is withdrawn (it never runs) and reports Failed, as does a helper-mode deployment that never submitted a request. Deployments without a helper request remain unknown and unreported after interruption, and are never re-applied. Killed or timed-out installer commands similarly have unknown completion; explicit exit codes are command results, never boot confirmation. See [configured installers and application recovery](PLATFORM_INSTALLERS.md).

A compromised daemon can only submit an image to that updater. Keep the updater's own image-signature verification enabled (for example SWUpdate signed images), and keep `fs.protected_hardlinks=1`, the systemd default. Recipe `install` scripts never reach root.

Migrating an existing device to these units:
- systemd re-owns `/var/lib/suifleet` to `suifleet` on start. The journal is preserved.
- Make `/etc/suifleet/config.toml` `root:suifleet 0640`.
- Install `/etc/suifleet/firmware.toml` as `root:root 0600` with the firmware command.
- Set `apply.mode = "privileged-helper"`, then `systemctl enable --now suifleet-firmware.path`.
- With `direct` mode, a root-only updater now fails as a privilege failure and reports Failed.

Verified locally (container, not hardware):
- Rust tests cover privilege/timeout/environment failures and helper attacks: swapped artifact, planted symlinks, invalid IDs, missing helper. They also cover the claim protocol: an update outliving the daemon's wait completes and is not Failed; an unclaimed request is withdrawn and never runs; a helper dying after its claim is indeterminate and later swept as interrupted; a live claim is not swept; late results stay until acknowledged; startup recovery keeps results and claims pending and withdraws unclaimed requests; the journal never settles indeterminate outcomes.
- `scripts/test_firmware_restart.py` (in `make test-ota`) restarts the real daemon over updates left with a written success result, a live claim, an abandoned claim, an unclaimed request, no request, and an older journal entry. The result and live claim are reconciled as Applied, never Failed. The abandoned claim becomes indeterminate and stays so across another restart, and the rest report Failed without the updater running.
- `scripts/test_ota.py --firmware-helper` covers the helper path. `--daemon-user dev`, run as root, covers the real daemon as an unprivileged user with the helper as root.
- `systemd-analyze verify` is clean (systemd 252).
- `systemd-analyze security --offline`: daemon 1.7 OK (was 9.6 UNSAFE); helper 5.5 MEDIUM, by design, for device access.

SF-025 verified the actual BitBake `useradd` package for `raspberrypi3-64` and booted the ARM64 QEMU image with provisioning/enrollment units; see [packaging evidence](YOCTO_PACKAGING.md#recorded-target-validation-2026-10-14). Physical Pi boot, DNS under `RestrictAddressFamilies`, and SWUpdate/RAUC under the helper sandbox remain unverified.

## Supervised components (SF-014)

A recipe with `lifecycle.run` is a supervised component rather than a one-shot update. Like `install`, it needs `apply.allow_recipe_commands = true` on the device; otherwise the deployment fails without running anything. Older daemons reject such recipes.

```json
"lifecycle": {
  "install": "chmod +x {artifact}",
  "run": "exec {artifact} --data {work}",
  "readiness": "test -e {work}/ready",
  "shutdown": "kill -TERM {pid}",
  "restart": "on-failure"
}
```

- **Substitutions** (shell-quoted): `{artifact}` is the verified artifact in the version's workspace, `{artifacts}` that workspace, `{work}` the component's persistent working directory (also its cwd and HOME), and `{pid}` (readiness/shutdown only) the run's process. Run, readiness and shutdown commands get the same cleared environment and limits as updaters, plus `SUIFLEET_COMPONENT`, `SUIFLEET_COMPONENT_VERSION` and the recovery marker `SUIFLEET_SUPERVISOR`.
- **Workspaces:** `<state_dir>/supervised/<name>/versions/<version>-<deployment>/` holds each version's artifact; `install` runs there while the old version keeps running. The committed and previous versions are kept; older ones are removed. `<name>/work/` persists across versions; output goes to `<name>/component.log` (mode 0600, rotated at 1 MiB).
- **Readiness:** with `readiness`, the command is retried every second until it succeeds or `runtime.component_ready_timeout_seconds` (60) passes; without it, a run that survives `component_startup_seconds` (3) is ready. A deployment is reported Applied only once its component is ready.
- **Replacement:** the old version is stopped, the new one started. If the new one never becomes ready, it is stopped, the previous version is started again, and the deployment is reported Failed with "rolled back to …". For opted-in applications, an explicit recovery command restores the retained artifact/configuration and readiness before the journal confirms the previous identity; interrupted installer work stays unknown. See [application recovery](PLATFORM_INSTALLERS.md).
- **Replacement without `run`:** a same-name version without a run command ends supervision. Its install runs while the supervised version keeps running; if the install fails, that version stays running and committed. Once it succeeds, the supervised version is stopped with its descendants and removed from `active.json` and `status.json` (its `work/` and log are kept), so no daemon start brings it back. If that cannot be recorded, the deployment is reported Failed.
- **Restart policy:** `on-failure` (default) restarts after a non-zero exit, a signal, or a failed readiness check; `always` also after a clean exit; `never` leaves it Exited. Backoff doubles from 1 s to 60 s (reset after a minute of stable running). More than `component_max_restarts` (5) restarts within ten minutes makes it Broken. Opted-in applications with a retained previous version run [explicit recovery](PLATFORM_INSTALLERS.md); otherwise it waits until the next deployment or daemon start.
- **Stopping:** the `shutdown` command (or SIGTERM) gets `component_stop_timeout_seconds` (10), then SIGTERM again and SIGKILL to the whole process group. Run commands must stay in the foreground: the daemon is a child subreaper and kills any descendant that left the group and was orphaned. The systemd unit uses `KillMode=mixed`: `systemctl stop` sends SIGTERM only to the daemon, which runs each component's shutdown (dependents first), and systemd then SIGKILLs whatever remains in the cgroup. With the default `control-group` mode, systemd would SIGTERM every component at once, bypassing their shutdown commands and ordering. Keep `TimeoutStopSec` (default 90 s) above the components' combined stop time.
- **Bootstrap after daemon/device restart:** the versions in `supervised/active.json` (written only after readiness) start on every daemon start. A crash mid-replacement therefore restarts the previous version, and the interrupted deployment remains unknown and unreported. Explicit application recovery has its own durable guard and fails closed across interrupted recovery; see [SF-016](PLATFORM_INSTALLERS.md). Runs left by a daemon that was killed outright are killed before anything starts again: the recorded runs (matched by PID and kernel start time) and their process groups, every process carrying the state root's `SUIFLEET_SUPERVISOR` environment marker (which survives setsid and reparenting), and all of their descendants (including ones that cleared their environment). They are frozen with SIGSTOP until the set stops growing, then killed. If any survives SIGKILL for 5 s, the daemon refuses to start.
- **Fail-closed restart after a crash:** a descendant that cleared its environment, started its own session and lost its parent chain is invisible to that recovery. So the daemon records itself in `supervised/instance.json` while it supervises components and removes the record only after stopping them all. Before it starts the first component process (an install or a run), it marks the record on disk. If it finds a marked record (or an unreadable one) from an instance that did not stop its components, even one killed during a first install that recorded no status yet, it starts only if no process of that instance can remain. That holds after a reboot (another kernel boot ID), or when the instance's cgroup v2 can't be left (no `cgroup.procs` from it up to the root is writable) and holds no process but the new daemon. Under the systemd unit (`KillMode=mixed`, which still kills the whole cgroup once the daemon is gone, and `ProtectControlGroups=yes`) this holds after every crash. Elsewhere, such as a container or a login session, it never does: the daemon kills the leftovers it can find, then refuses to start, and keeps refusing. To recover, reboot, or check that no process started by a component remains and delete `supervised/instance.json`. A record from a daemon that is still running (same PID and start time) also stops a second instance before it touches anything.
- **Runtime state:** `supervised/status.json` holds each component's version, deployment, phase (`starting`, `running`, `backoff`, `stopping`, `stopped`, `exited`, `broken`), PID, restart count, last exit and transition time. It is local. With `[telemetry]`, the daemon reports it off-chain in signed snapshots, together with installed versions and sync health ([TELEMETRY.md](TELEMETRY.md)); chain receipts stay per deployment.
- **Sandbox:** components run inside the daemon's systemd sandbox (`ProtectSystem=strict`, same user), so they can write only the state directory.
- **Dependencies (SF-012):** a dependency recipe with `run` is supervised too. Dependencies are installed and become ready before their dependents start, and bootstrap starts committed components in dependency order. A dependent is not stopped when a dependency fails.
- **Bootstrap and systemd:** the daemon reports ready to systemd (and starts its watchdog) before bootstrap, which can take up to a readiness timeout per committed component. Unready components therefore cannot exceed the unit's start deadline and restart the daemon into the same bootstrap; deployment polling, which can deliver a repair, starts once bootstrap finishes. A stop during bootstrap stops every component already started.
- **Not yet:** configured component bootstrap steps (system reboot remains platform-owned) and coupling a dependency's failure to its dependents. Component configuration is described below.

`make test-ota` runs `scripts/test_supervisor.py`: the real daemon against the simulator, through readiness, a crash restart, an unready replacement rolled back, a graceful replacement, daemon SIGTERM with grouped and escaped descendants, a daemon restart, a daemon SIGKILL (its successor kills the setsid and environment-cleared leftovers, then refuses to start over a descendant that also cleared its environment, started its own session and was reparented, until the record is cleared), and a one-shot version replacing the supervised one. Hardware reboot and power loss are not covered.

`make test-supervisor-systemd` (on the host; needs Docker) runs `scripts/test_supervisor_systemd.py`: the shipped `suifleet.service` and `suifleet-provision.service`, unchanged, under real systemd (PID 1) on cgroup v2 in a disposable privileged container, against the simulator. The run's grouped, setsid, environment-cleared and reparented environment-cleared setsid descendants are all in the unit's cgroup. A component crash restarts the run and kills all of them. A daemon SIGKILL makes systemd empty the cgroup and restart the unit; the successor logs that none of the previous instance's processes can remain (sealed cgroup), bootstraps the component, and exactly one instance runs with no earlier process left. `systemctl stop` runs the component's own shutdown command (not just a signal from systemd), records it as stopped, and leaves the cgroup empty. With two committed components that never become ready (60 s readiness timeout each), the unit stays active with no start timeout or restart while bootstrap outlasts the 90 s start deadline, then the same daemon applies a repair deployment; `systemctl stop` during bootstrap stops the started and the still-starting component and leaves the cgroup empty. Recorded run: [`validation/sf014-systemd.json`](validation/sf014-systemd.json). Readiness checks in a persistent work directory should be bound to the current run (for example, compare a marker with `{pid}`), or a stale marker can declare a new run ready early.

## Component configuration (SF-015)

Recipe schema `1.2` adds a configuration to any component with an `install` or `run` command (supervised or one-shot, root or dependency). Older daemons reject 1.2 recipes; 1.0/1.1 recipes must keep `"configuration": {}`.

```json
"lifecycle": { "run": "exec {artifact} --config {configuration}", "validate": "{artifact} --check {configuration}" },
"configuration": {
  "defaults": { "port": 8080, "log": { "level": "info" }, "db": { "host": "db1" } },
  "reset": ["/log/level"],
  "merge": { "port": 9000 },
  "confidential": ["/db/password"]
}
```

- **Effective configuration:** each install computes the component's next configuration from the one committed for it (none before its first install), in this order. (1) `defaults` fill in what is missing; values the device already has are kept, so a new version's changed default applies only once reset. (2) Each `reset` [JSON pointer](https://www.rfc-editor.org/rfc/rfc6901) takes its default value again, or is removed if the defaults have none; `""` resets the whole configuration. (3) `merge` is applied recursively: objects merge key by key; arrays, scalars and `null` replace. (4) `confidential` pointers are removed. Pointers address object members only; one that passes through an array or scalar fails the deployment.
- **Persistence:** `<state_dir>/configuration/<name>/committed.json` (0600, directory 0700) holds the deployment, version, defaults and effective configuration of what the component runs now. An install stages `staged.json` first. It is committed once the install is confirmed (or the supervisor committed the version), kept while a privileged update's outcome is unknown, and discarded otherwise, including after a daemon crash. A failed deployment or rollback therefore leaves the previous configuration in place. After an application recovery restores a previous supervised version, `committed.json` is restored to the record that version was deployed with (removed for a version that predates configuration management), so later updates build on it rather than on the failed version's. Configuration changes only through deployments; the [local IPC](COMPONENT_IPC.md) reads committed configuration and installs authenticated configuration deployments; it does not bypass deployment validation with local edits.
- **Delivery:** the component receives the effective configuration plus its device overlay as a 0600 JSON file: `{configuration}` in `install`, `run`, `readiness`, `shutdown` and `validate`, and `$SUIFLEET_CONFIGURATION` in supervised commands. For a supervised component it lives in the version's workspace and is rewritten on every start, so a rollback restores the previous version's configuration and a rotated overlay value applies on the next restart. A one-shot component's install gets a temporary copy and must keep what it needs. A redeployment of the same version with a new configuration restarts a supervised component with it; components can poll their public committed configuration over [IPC v1](COMPONENT_IPC.md); no in-place mutation of running configuration is performed.
- **Validation before apply:** after every artifact of the plan is verified and before the first install, the daemon computes and checks each configuration (pointers, at most 4096 values and depth 32, provisioned confidential values) and runs the recipe's `validate` command, if any, against a candidate file. Exit 0 accepts. Exit 75 defers: nothing changes and the deployment is retried on later polls, for at most `components.configuration_defer_max_seconds` (86400) after the first deferral. Any other exit, or a timeout (`apply.timeout_seconds`), rejects it and the deployment is reported Failed with nothing changed. `validate` needs `apply.allow_recipe_commands`; it runs in the component's `{work}` directory (where a running instance can leave state for it) with `{artifact}`, `{artifacts}` and `{configuration}` substituted, and its output goes to `<state_dir>/supervised/<name>/component.log`. So does the output of every other lifecycle command (`install`, for install-only components too, `run`, `readiness` and `shutdown`), since each receives the materialized configuration; the daemon log never carries it.
- **Confidential values:** recipes are public (Walrus). Values at `confidential` pointers come only from the device overlay `components.configuration_overlay_dir/<name>.json` (default `/etc/suifleet/component-config`), a JSON object of pointer to value such as `{"/db/password": "…"}`. The file must not be accessible to other users (`chmod o-rwx`); the daemon refuses it otherwise. A deployment whose confidential pointer is not provisioned fails before anything changes. Both clients reject a recipe with a value at, or a non-object above, a confidential pointer. Committed records, the journal, status reports and the daemon log hold no overlay values, and errors name pointers, never values. Overlay values reach only the 0600 materialized configuration files, the 0600 set of values to redact from logs (`<state_dir>/logs/confidential.json`, see [LOGS.md](LOGS.md)) and whatever the component's own commands write, including their output in the 0600 `component.log`. The staged and committed records and each version's workspace are fsynced, directory entries included, before an install starts or a record is committed. The overlay may also set non-confidential pointers; it is applied last. Seal-encrypted recipes ([CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md)) hide recipe content from Walrus readers, but configuration secrets still come only from the overlay: the rule above applies to every recipe, and recipe digests are public.
- **Catalog versions:** a catalog version's recipe is immutable, so its `merge`/`reset` apply each time that version is deployed. A per-deployment configuration update for catalog deployments would need an on-chain field on the deployment record; schema-1 deployments carry their own recipe and therefore their own update.

`make test-ota` runs `scripts/test_configuration.py`: the real daemon against the simulator, through defaults with an overlay-provided confidential value, a merge and a version upgrade with reset, an invalid reset, a rejecting validate command and an unprovisioned confidential value (each Failed with nothing changed), a deferral that later applies and one that expires, a daemon restart with a rotated overlay value, install scripts, a failing then passing readiness check and a shutdown command printing their configuration on stdout and stderr (supervised and install-only) with that output only in their 0600 component log, a one-shot component's install receiving its configuration, a deferred deployment deleted on chain (dropped, deferral pruned), and a scan proving no overlay value appears in state, the journal or the daemon log.

## Artifact storage and downloads

Walrus stores each recipe and artifact for a fixed number of epochs. After the end epoch, devices that have not applied a deployment can no longer download it.

**When you upload** (dApp):
- Uploads use `NEXT_PUBLIC_WALRUS_EPOCHS` (default 5).
- They pass `send_object_to=<connected wallet>`, so the operator owns the Walrus `Blob` object and is the one who can renew it.
- The upload screen shows the end epoch, the cost in FROST and the renewal command (`walrus extend --blob-obj-id <id> --epochs-extended <n>`).
- It warns when the cost exceeds `NEXT_PUBLIC_WALRUS_MAX_COST_FROST`.
- It warns when the bytes were already certified by someone else. Then you don't own the object and cannot renew it.
- It warns when the publisher kept the object, or reported no end epoch.

**Monitoring** (dApp): the deployments page probes each deployment's recipe and artifact through the aggregator. It flags any blob that returns 404/410 as expired or unavailable. Re-upload and redeploy, or extend storage beforehand.

**Downloading** (daemon):
- Bytes are written to `<artifact>.partial` and renamed only when complete. The SHA-256 is computed over the complete file and checked before applying.
- A dropped connection, timeout, 5xx or 429 is retried up to `walrus.download_attempts` times with backoff. Each retry resumes with HTTP `Range` and verifies `Content-Range`, or restarts from zero if the aggregator ignores the range.
- Each blob must finish within `walrus.download_timeout_seconds` (default 3600) across all attempts, otherwise the deployment reports Failed. The per-read timeout alone cannot catch an aggregator that trickles bytes, and deployments are processed one at a time, so a stalled transfer would otherwise block every later deployment. Raise it for large artifacts on slow links.
- A 404/410 is permanent: the deployment reports Failed with "unavailable or expired" and is not retried.
- Before writing, the daemon refuses artifacts over `walrus.max_artifact_bytes`, and transfers that would leave less than `walrus.min_free_bytes` free. When the aggregator sends no `Content-Length` (chunked), both limits are enforced while streaming: free space is re-checked whenever the bytes written since the last check would exceed what the reserve allows, and the partial file is deleted on refusal.
- The artifact cache holds only the in-flight artifact. Each deployment's working directory is removed afterwards, and leftovers from a crash are removed at startup. Interrupted deployments are never re-applied.

Verified locally:
- Rust tests against a scripted aggregator cover range resume, an ignored range, transient retry, giving up without a final file, an expired blob, the size limit, insufficient disk, chunked bodies against the size limit and disk reserve, the whole-transfer deadline against a trickling server, and GC.
- The OTA simulator checks resuming from byte 10, an expired artifact reporting Failed without running the updater, and an empty cache afterwards.
- The publisher-response parsing and warnings were checked against the documented response shapes.

Not verified:
- Actual Walrus testnet retention, renewal with `walrus extend` and `send_object_to` ownership. These need uploads to a public network.
- Whether the public aggregator honors `Range`; the daemon handles both cases.
- The dApp cannot show epochs remaining until it reads the current Walrus epoch, so it reports the end epoch only.

## Troubleshooting without losing state

For customer fleet recovery from a clean operator machine, dependency outages, frontend rollback, compromised devices, payment failure and customer exit, follow [FLEET_RECOVERY.md](FLEET_RECOVERY.md) (`make test-fleet-recovery-drill`, `make live-fleet-recovery`).

- Docker unavailable/permissions: fix host daemon access; do not invoke Docker inside the dev container.
- Missing Python in an old image: rebuild the current Dockerfile; `scripts/test_ota.py` requires Python 3.
- Wallet/SDK type skew: inspect the dependency graph; migrate/deduplicate the supported version set, not `any` casts.
- Missing required cap/digest fields: follow the documented migration/reissue process; do not claim an upgrade filled existing objects.
- Persistent report failures: inspect effects, target membership, chain/IDs and gas. The journal preserves pending reports.
- Stale frontend IDs: regenerate config and rebuild the static app.
- `make down`: removes cache and wallet volumes; use only when deliberately discarding them, after preserving required state.

## Deployment admission controls (SF-018)

The deployment creator can pause/resume admission, permanently cancel admission,
and set UTC Unix-millisecond not-before, expiry, a single absolute maintenance
window `[start,end)`, and an advance-notice duration (at most seven days).
Zero disables the respective deadline/window/notice. These are admission times,
not promises that an update finishes before the window closes. A closed window
waits for an operator to edit it; it does not recur automatically. Expiry and
cancellation observed by a device are terminal: create a new deployment to retry.
Removing a desired component or deleting a record remains distinct from cancellation.

On **Deploy component**, select **Create paused** to publish the control in the
same transaction as the deployment (including catalog and desired-state creation).
Then use **Deployments → Edit schedule / notice**, save while paused, and resume.
This prevents immediate admission before a schedule is configured. Pause, resume,
cancel and schedule editing require the creator's wallet; deployer roles do not
grant control authority. The UI displays the control state, actionable reason,
UTC times and notice. It does not imply every device has received the change:
check device logs/journals and authoritative final receipts separately.

The daemon persists notices and deferrals in `state.json` separately from apply
outcomes. A deferred one-shot deployment survives event-cursor advancement and
restarts. Desired-state deferrals are reconsidered under the current group plan;
a superseded revision is never selected from the deferred queue. The notice timer
starts when this device first observes a control revision and survives restarts;
editing controls (including resume) starts a new notice. Notices are structured
local log messages before disruptive work, not an interactive approval prompt.
Devices need a synchronized UTC clock. Schedule/window checks use current UTC,
so correcting a forward clock step restores later admission decisions. A notice
timestamp ahead of corrected UTC restarts that device's notice interval. Per-record
terminal cancellation/expiry observations survive clock correction and control edits;
correct a bad clock before creating a replacement for an already expired record.
Extending an expired record can split the fleet: devices that observed expiry keep
it terminal, while devices that did not may admit it. Create a replacement instead.
No offline or cached admission is allowed.

Controls are checked before preparation, then read again after all artifacts are
verified and immediately before journaling apply intent. Read/validation failures
fail closed. The last successful read is the **admission boundary**: later pause,
cancel, expiry or window closure does not interrupt the admitted dependency plan,
kill its updater, roll it back or change its actual success/failure receipt. A
control transaction racing after that read can therefore coexist with a successful
apply. A crash after intent retains the existing no-reapply/helper reconciliation
rules. There is no distributed atomic transaction spanning Sui and a local updater.

This is an additive compatible contract upgrade: record layouts/signatures remain
stable. `deployment_manager::ControlKey` and `Control` originate in the upgrade;
regenerate manifests and both clients rather than assigning their original package
origin. Absence of the field preserves immediate admission on existing records.
Deploy daemon **0.2.1** to the fleet before relying on controls. Newly generated
manifests require edge >= 0.2.1 and reject earlier daemons; older daemons
cannot enforce fields introduced later. Old package entry points remain callable;
status writes do not change controls, and deletion still prevents later object
reads. Do not interpret deleting a record through an older package as acknowledged
cancellation or retain a deleted record as an apply authorization.

Validation commands (disposable network only for the final two):

```bash
make test-contracts
make check-interfaces
make edge-test
make test-ota                         # includes SF-018 race/restart tests
make dapp-typecheck
make dapp-build
make test-deployment-control-localnet # start isolated loopback node/faucet first
node scripts/test_frontend_live.cjs   # actual gRPC control builders/BCS reads
```

The simulator covers pause/resume across restart/cursor advancement, offline
expiry/cancel, not-before plus notice, pause/cancel/expiry/window closure during
fetch, window editing and cancellation during an admitted updater. The local-chain
test upgrades main's package with an existing record, checks distinct control type
origins, old-package calls, irreversible cancellation and cleanup. These do not
establish public-network interoperability, real extension-wallet acceptance or
physical firmware/power-loss safety.

## Optional AWS component credentials

See [AWS_CREDENTIALS.md](AWS_CREDENTIALS.md) for the separately provisioned X.509
IoT role-alias adapter, dedicated component UIDs, per-start exec wrapper, refresh
and revocation behavior, and `make test-aws-credentials`. This opt-in companion
does not use the Sui/Seal identity or grant shared-UID supervised processes access.
Live AWS account/service acceptance remains unauthorized and unrun.

## Local support console (SF-079)

See [LOCAL_CONSOLE.md](LOCAL_CONSOLE.md). The optional `[console]` serves an
authenticated, audited diagnostic API/UI on a loopback address only; reach it
remotely with `ssh -L 1441:127.0.0.1:1441`. Generate tokens with
`suifleet --console-token` and configure only their hashes. Support operations
are limited to the enabled `start`/`stop`/`restart` set. The audit log is
`<state_dir>/console/audit.jsonl`; preserve it with the journal.

## Staged rollouts (SF-017)

See [staged rollout operation and recovery](STAGED_ROLLOUTS.md) for immutable cohorts,
creator-signed admission/health ticks, rate/concurrency/bake limits, offline slots,
audited pause/abort and schema-3 manifest/upgrade requirements.

OCI container components use the existing component supervisor with a pinned-image descriptor; see [OCI_COMPONENTS.md](OCI_COMPONENTS.md) for engine provisioning, boundaries and offline recovery.

The opt-in secure tunnel destination companion, its separate service identity,
installation, revocation controls and offline real-proxy tests are documented in
[SECURE_TUNNELING.md](SECURE_TUNNELING.md). AWS account/service and physical
acceptance remain unrun in SF-031/#32 and SF-087/#159.

### Direct-chain fleet data

For the optional Sui/Walrus backend, follow [serverless fleet data](SERVERLESS_FLEET_DATA.md).
Install the checksum-bound keyless Node/WASM codec artifact, configure public
fleet/writer/budget/storage IDs and matching isolated-signer policy, and preserve
`state_dir/fleet-data` during upgrades. The ordinary HTTP telemetry/log endpoints
are unnecessary in this mode. Do not remove a pending transaction or ciphertext
to force a fresh upload: reconcile retained effects first. Owner prepaid storage,
revocation and cadence remain enforced on chain. A static export or simulator run
does not establish live Site deployment, certification or production acceptance.

## Check cadence and endpoint fallback (SF-123)

`[runtime] poll_seconds` defaults to 1800 (30 minutes); customers may set
1..=86400 seconds, including 3600 for hourly checks. `rollout_poll_seconds`
defaults to 60, accepts the same bounds, and is capped at `poll_seconds`.
Known active maintenance windows and deployments waiting for rollout eligibility
use the faster cadence. An observed active calendar window remains in bounded
journal metadata until its recorded end/expiry, even after an update completes
and across daemon restarts. Overlapping observed periods coalesce; this clock
metadata never grants admission authority. This is a local fallback until SF-130 supplies fleet settings.
An undiscovered deployment or remotely changed control is observed at the next
check; local timers cannot anticipate unpublished changes.

After discovering controls, the daemon arms a local wakeup for the latest of
scheduled start, maintenance-window start and durable advance-notice completion.
Thus a known window shorter than 30 minutes can still be entered. Restarts
reconstruct deadlines from the journal. Every wake re-reads identity, targeting,
controls and grants; a timer grants no authority. Paused, terminal and impossible
windows do not cause a tight loop. Failures use exponential equal jitter with
millisecond randomness (initially 1–2 seconds, capped at one hour or twice the
configured check interval, whichever is lower); a successful check resets it.
Discovery/reconciliation failure backoff takes precedence over control wakeups
during an outage. Receipt-delivery failures stay queued and logged independently;
they do not fail an otherwise successful check or suppress local timers, even
when a deleted deployment or lost targeting makes delivery permanently impossible.

The manifest may include `rpc_urls`, an ordered list of at most eight gRPC v2
endpoints. `[sui] rpc_urls` is an optional local override. An empty or omitted
list falls back to the existing manifest `rpc_url`, preserving older manifests.
Newly generated manifests include a one-element list. For pilots put Mysten's
public endpoint first (testnet `https://fullnode.testnet.sui.io:443`), followed by
an explicitly configured provider or your own indexed node. No provider is
purchased or automatically discovered.

Before using a candidate the daemon verifies its chain identity. A missing,
malformed or mismatched identity is a hard rejection, never a reason to try a
different chain. Unavailable, rate-limited (including HTTP 429), and timed-out
calls try the next endpoint in order. Calls and whole event pages have a
30-second timeout per candidate; partial event pages are discarded before
retrying, so no cursor advances over missing frames. Simulation retries rebuild
the same intended call and retain verify-before-sign checks. Execution retries
submit the same signed bytes; effects still determine success. Configure endpoints
without credentials in URLs and ensure every candidate retains the required
indexed history. Other authorization, data and execution errors fail closed.

Set staged rollout `offline_timeout_ms` **above the normal check interval plus
worst-case download/apply/report time** (and allow for network/backoff delays).
Do not keep the old few-second stall timeout when normal checks take 30 minutes.
