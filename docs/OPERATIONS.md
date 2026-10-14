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

`make check` runs Move tests, Rust tests, the actual-daemon OTA simulator, frontend typecheck and static production build. It never publishes contracts or flashes hardware.

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
- A 404/410 is permanent: the deployment reports Failed with "unavailable or expired" and is not retried.
- Before writing, the daemon refuses artifacts over `walrus.max_artifact_bytes`, and transfers that would leave less than `walrus.min_free_bytes` free. When the aggregator sends no `Content-Length` (chunked), both limits are enforced while streaming: free space is re-checked whenever the bytes written since the last check would exceed what the reserve allows, and the partial file is deleted on refusal.
- The artifact cache holds only the in-flight artifact. Each deployment's working directory is removed afterwards, and leftovers from a crash are removed at startup. Interrupted deployments are never re-applied.

Verified locally:
- Rust tests against a scripted aggregator cover range resume, an ignored range, transient retry, giving up without a final file, an expired blob, the size limit, insufficient disk, chunked bodies against the size limit and disk reserve, and GC.
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
