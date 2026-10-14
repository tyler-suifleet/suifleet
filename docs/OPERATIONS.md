# SuiFleet operations and verification

This runbook describes current local operations and the intended release gates. It does not establish production readiness. Current release blockers include retired JSON-RPC integration, incorrect origins for newly introduced key types, and old-package entry points that can bypass new checks on shared authoritative state.

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

`scripts/check_contract_interfaces.py` (`make check-interfaces`) compiles a summary of `contracts` and fails when the dApp, daemon or deployment manifests reference a Move module, function or type that no longer exists, is not transaction-callable, or (for dApp `moveCall`s) takes a different number of arguments. It checks references after a package placeholder (`${PACKAGE_ID}::module::name`, `{}::module::Name`) and address-free type names such as `"device_cap::DeviceAddressKey"` used for per-type origins. An unknown project module is an error; address-free names are skipped only when they belong to a Sui/Move framework module. It also fails if it finds no references at all. `make test-interfaces` runs its offline regression tests. It does not check argument types, event fields or recipe schemas; that is SF-005.

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

The historical baseline has 13 passing Move tests and 10 Rust tests; counts can legitimately change. Verify results for the actual commit rather than asserting those numbers as a release requirement. Simulator mocks are not proof of network transaction building, wallet compatibility, package upgrades or post-reboot health.

If Rust files change, run `cargo fmt --manifest-path suifleet-edge/Cargo.toml` with rustfmt installed, then rerun relevant tests. The minimal container toolchain may need `rustup component add rustfmt` first. Do not silently upgrade the toolchain to make failures disappear.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and push to `main`. It builds the dev image from the pinned Dockerfile and runs `make ci` inside it: frozen pnpm install, Move tests, drift-gate regression tests, interface drift check, Rust tests, the OTA simulator, typecheck, static build and `git diff --check`. Each step's output and `toolchain-versions.txt` are uploaded as the `ci-logs` artifact (14 days). The job has a read-only token and no secrets; it never publishes, spends gas or touches hardware.

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
- Devices on the current daemon keep reporting through `report_status`/`report_status_for_group`, whose signatures are unchanged.

Recovery:
- Legacy unbound caps cannot report. Reissue them with `device_cap::issue_cap`.
- Legacy records show no authoritative status until the device reports again. Redeploy if an operator needs a verified receipt.

Local acceptance (throwaway localnet in the dev image; refuses non-localhost RPC):

```bash
git archive <prior-ref> contracts | tar -x -C /tmp/v1
SUI_CONFIG_DIR=/tmp/sui-config sui start --with-faucet --force-regenesis &
SUI_CONFIG_DIR=/tmp/sui-config sui client switch --env local
SUI_CONFIG_DIR=/tmp/sui-config python3 scripts/upgrade_security_localnet.py --v1 /tmp/v1/contracts --v2 contracts
```

It publishes the prior package, creates devices and deployments (including a legacy record with only forged statuses), then upgrades. Next it attacks through old and new entry points, requiring each attack to abort at the intended check. It also verifies a later version-bumped upgrade locks out this package, then resolves a deployment manifest and runs the real dApp `fetchDeploymentEvents` against new, legacy and forged-only records. Results come from on-chain effects, object state and events. It needs `dapp/node_modules` (`make dapp-install`). This does not replace an upgrade test against an existing deployment on a public network.

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

## Troubleshooting without losing state

- Docker unavailable/permissions: fix host daemon access; do not invoke Docker inside the dev container.
- Missing Python in an old image: rebuild the current Dockerfile; `scripts/test_ota.py` requires Python 3.
- Wallet/SDK type skew: inspect the dependency graph; migrate/deduplicate the supported version set, not `any` casts.
- Missing required cap/digest fields: follow the documented migration/reissue process; do not claim an upgrade filled existing objects.
- Persistent report failures: inspect effects, target membership, chain/IDs and gas. The journal preserves pending reports.
- Stale frontend IDs: regenerate config and rebuild the static app.
- `make down`: removes cache and wallet volumes; use only when deliberately discarding them, after preserving required state.
