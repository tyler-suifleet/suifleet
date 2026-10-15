# SuiFleet operations and verification

This runbook describes current local operations and the intended release gates. It does not establish production readiness. Current release blockers include the publication resolver's retired JSON-RPC integration (SF-006; the daemon and frontend use gRPC), incorrect origins for newly introduced key types, and old-package entry points that can bypass new checks on shared authoritative state.

## Environment

- Host: Linux/WSL, Git, Docker Engine/Compose and GNU Make.
- Container: Ubuntu 22.04, pinned Sui CLI and Rust, Node 22, pnpm and Python 3. Read exact build arguments in `Dockerfile`.
- `make dev` builds/starts `dev`; `make shell` opens it. Source is bind-mounted at `/workspace` under a host-UID/GID-matched user.
- Browser frontend: `http://localhost:3000` after `make dapp-dev`.
- Cache/wallet volumes: `cargo-registry`, `cargo-git`, `pnpm-store`, `move-cache`, `sui-config`.
- Device service: `suifleet.service`, config/key in `/etc/suifleet`, journal under `/var/lib/suifleet`; service provisioning is idempotent.
- Sui authorizes deployments/reports, Walrus holds recipes/artifacts, and the device invokes an operator-configured updater. No production indexer, coordinator, health ingress, secrets service or hosting deployment is currently recorded.

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

`.github/workflows/ci.yml` runs on every pull request and push to `main`. It builds the dev image from the pinned Dockerfile and runs `make ci` inside it: frozen pnpm install, Move tests, drift-gate regression tests, interface drift check, Rust tests, the OTA simulator, the test-component checks, typecheck, static build and `git diff --check`. Each step's output and `toolchain-versions.txt` are uploaded as the `ci-logs` artifact (14 days). The job has a read-only token and no secrets; it never publishes, spends gas or touches hardware.

All toolchain inputs are Dockerfile build arguments: base image digest, Sui release and SHA-256, Rust, Node package version and pnpm. Bump them deliberately, in their own commit, together with lockfile and compatibility-test updates; CI then runs against the new image. Workflow actions are pinned to commit SHAs.

Regressions these gates have been shown to catch: renamed/re-shaped Move functions and types used by clients (interface check), treating failed execution effects as success (Rust test and simulator), and reapplying journaled deployments after a crash before the cursor is saved (simulator).

### Live gates

`.github/workflows/live-gates.yml` is the only path for live checks: `package-upgrade`, `wallet` or `hardware`. It runs only from a manual dispatch that repeats the gate name. Each gate deploys through the GitHub environment `live-<gate>`, which holds that gate's secrets and must require a human reviewer. A preflight job refuses to run if the environment is missing or has no required reviewer. The hardware gate runs on a self-hosted runner labelled `raspberrypi3bplus`. The suites themselves are `make live-gate-<gate>` targets supplied by SF-010 and SF-031; until then a dispatched gate fails with that message.

Configure each environment once, as a repository admin:

```bash
for gate in package-upgrade wallet hardware; do
  gh api -X PUT repos/tyler-suifleet/suifleet/environments/live-$gate --input - <<JSON
{"prevent_self_review": true, "reviewers": [{"type": "User", "id": <human-reviewer-user-id>}]}
JSON
done
```

Model agents share the `tyler-suifleet` GitHub account, so the reviewer must be a separate human account with repository access, and `prevent_self_review` must stay enabled.

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

## Deployment process

1. Determine the explicit deployment scope and target network; verify the RPC's chain identity, signer address, gas and UpgradeCap ownership. Environment aliases alone are insufficient, especially after devnet resets.
2. Validate the release compatibility matrix and all relevant checks above. Resolve unsupported transport and upgrade-security blockers before a public-network release.
3. Prepare migrations for existing capabilities, recipes and state. Added dynamic fields do not appear automatically on old objects. New type keys can originate in the upgrade package rather than the initial package.
4. Publish an initial package or perform a compatible upgrade. Existing public signatures/layouts/abilities must remain compatible; an initializer is not a migration. Record execution effects, transaction digest, latest call ID, per-type origins, shared object IDs and UpgradeCap ID.
5. Generate frontend and device configuration from a network deployment manifest. This generator/manifest is planned; currently `.env.local` and device TOML are separate manual outputs.
6. Rebuild the frontend after environment changes. `NEXT_PUBLIC_*` variables are compiled into the static export; editing server environment after build does not rewrite those assets.
7. Stage daemon rollout, verify enrolled identity/cap binding and preserve each device's journal. Do not force firmware replay by deleting state. Run a real end-to-end update/report and negative-path acceptance test before expanding the rollout.
8. Record artifacts, versions, IDs, migrations, results and recovery instructions in the release issue. Keep private credentials out of all artifacts.

The current development command is:

```bash
make publish-contracts
```

It runs `scripts/redeploy.sh` and mutates the chain. It is not part of `make check`. The helper supports devnet/testnet aliases, parses metadata with unsafe unscoped greps and suppresses some publish/parse errors. It writes `dapp/.env.local`, not device config. It requires hardening and live validation before automated releases.

Current frontend variables are `NEXT_PUBLIC_PACKAGE_ID`, `NEXT_PUBLIC_ORIGINAL_PACKAGE_ID`, `NEXT_PUBLIC_REGISTRY_ID`, and `NEXT_PUBLIC_NETWORK`. The blanket original-ID model must become a per-type-origin mapping for upgraded packages. Current edge configuration is described in `suifleet-edge/config.example.toml`; its `rpc_url` is legacy and must migrate.

## Static frontend and Linux deployment

`pnpm build` produces `dapp/out`; serve that directory through the selected static host. Walrus Sites is an intended option, but no hosting account/site or current site-builder version is verified here. Select/validate its current publish procedure in a hosting issue before publishing.

Install the daemon, systemd service and real config through the validated image/package workflow. The service invokes `--provision` before startup and creates its state directory. Register the printed address, record Device/DeviceCap IDs, and fund report gas. Apply success currently means updater exit code zero, not post-boot health.

The Yocto layer is `suifleet-edge/meta-suifleet`; it is a scaffold needing an actual build. `SRCREV = AUTOREV` and missing cargo dependency integration must be addressed before calling the image reproducible. SWUpdate/RAUC and A/B recovery remain external adapter work.

## Execution privileges

| Principal | Runs | Can write | Cannot |
|---|---|---|---|
| `suifleet` user (`suifleet.service`) | Daemon: chain polling, downloads, journal, recipe `install` scripts | `/var/lib/suifleet` only | Change its config or the helper config, use capabilities, create namespaces, see other processes, exceed 256 MiB / 64 tasks |
| root (`suifleet-firmware.service`, path-activated) | Only the command in `/etc/suifleet/firmware.toml` | Devices and system partitions, as the firmware updater needs; its own `/var/lib/suifleet-firmware` | Use the network, accept a command from the daemon |
| root (`ExecStartPre=+`) | Key provisioning and handing the key to `suifleet` | `/etc/suifleet/device.key` | — |

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
- On startup, a deployment the previous run left without a status is settled from the spool before anything else reads the journal. A written result or a claim keeps it pending for the collector. An unclaimed request is withdrawn (it never runs) and reports Failed, as does a helper-mode deployment that never submitted a request. Deployments without a helper request keep the direct-mode policy: an interrupted apply reports Failed and is never re-applied.

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

Not yet verified: a BitBake build with the `useradd` class, and the units on the Raspberry Pi image. That includes DNS under `RestrictAddressFamilies`, and SWUpdate/RAUC under the helper sandbox.

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

- Docker unavailable/permissions: fix host daemon access; do not invoke Docker inside the dev container.
- Missing Python in an old image: rebuild the current Dockerfile; `scripts/test_ota.py` requires Python 3.
- Wallet/SDK type skew: inspect the dependency graph; migrate/deduplicate the supported version set, not `any` casts.
- Missing required cap/digest fields: follow the documented migration/reissue process; do not claim an upgrade filled existing objects.
- Persistent report failures: inspect effects, target membership, chain/IDs and gas. The journal preserves pending reports.
- Stale frontend IDs: regenerate config and rebuild the static app.
- `make down`: removes cache and wallet volumes; use only when deliberately discarding them, after preserving required state.
