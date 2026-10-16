# SuiFleet

Decentralized edge software and firmware deployment using Sui contracts, Walrus storage, a Next.js wallet app and a Rust Linux daemon.

**Status:** locally testable one-shot OTA foundation. Greengrass OTA/fleet feature compatibility is the product goal, not the current release claim. See [the feature audit](docs/GREENGRASS_PARITY_AUDIT.md) and [delivery plan](PLAN.md).

**Transport:** the edge daemon and the web app use the supported Sui gRPC APIs (tested on real local networks). Local tests do not prove public-network, extension-wallet or hardware compatibility.

## Implemented flow

1. The operator registers a Device and issues a DeviceCap to the device's address in one wallet transaction.
2. The app uploads an artifact and a schema-v1 recipe to Walrus. The deployment references the recipe, stores a digest of the exact recipe bytes and the artifact SHA-256, and targets devices/groups.
3. The daemon polls events using the original package/type origin, retaining an atomic cursor on disk. It verifies the current Device owner, signing address and live group membership.
4. It reads the recipe, validates its on-chain digest and component identity/hash/schema, downloads the referenced artifact, checks its SHA-256, and invokes the configured one-shot updater.
5. It journals the result before submitting a correctly signed status transaction. Failed status submission retries across restarts. The app reads per-device statuses from the deployment's dynamic fields.

Direct reports require a targeted DeviceCap. Group reports additionally require a targeted creator-owned group with current device membership. Reports also require the registered signing address bound to the capability. Software-key authentication does not provide hardware attestation.

## Business model

See the [network-first business plan](docs/BUSINESS_PLAN.md) for the usage-cost opportunity, dated Mainnet fee evidence and adjacent product assessment. Customer pricing and full production margins remain unvalidated.

## Development

```bash
make dev
make shell
make test-contracts
make dapp-install
make dapp-build
make edge-test
make test-ota
```

Docker and GNU Make are required on the host. Make targets execute directly inside the development container and through Docker Compose on the host. `make down` removes development containers **and named volumes**, including development wallet state.

The locked frontend uses a Sui SDK version compatible with its legacy wallet library. Updating it to a supported transport requires migrating those packages together.

## Software provisioning

```bash
# First boot: creates a 0600 key, never overwrites an existing key.
suifleet --provision /etc/suifleet/device.key
suifleet --derive-address /etc/suifleet/device.key
```

The systemd service runs provisioning before startup and creates `/var/lib/suifleet` for state. Register the printed address in the app, record the Device/DeviceCap IDs, and fund the device address for report gas. Fill in `suifleet-edge/config.example.toml` and install it as `/etc/suifleet/config.toml`.

Sui address derivation is `Blake2b-256(0x00 || ed25519_public_key)`. Devices enrolled using this project's former SHA3 derivation need re-enrollment. TPM/HSM signing, attestation and key rotation/revocation are not implemented.

```toml
[sui]
rpc_url = "http://your-compatible-legacy-rpc"
package_id = "0x<LATEST_PACKAGE_ID>"
original_package_id = "0x<ORIGINAL_PACKAGE_ID>"

[device]
keypair_path = "/etc/suifleet/device.key"
device_object_id = "0x<DEVICE_ID>"
device_cap_id = "0x<CAP_ID>"

[walrus]
aggregator_url = "https://aggregator.walrus-testnet.walrus.space"

[apply]
command = "/usr/bin/swupdate -i"
timeout_seconds = 600
allow_recipe_commands = false

[runtime]
state_dir = "/var/lib/suifleet"
poll_seconds = 10
replay_history = false

[components]
offline_candidates = "never"   # or "cached"; see docs/COMPONENT_DEPENDENCIES.md
```

Fresh enrollment skips historical events; an existing journal always catches up from its cursor. `replay_history = true` is only for deliberate historical job replay. Use separate state directories for different networks/packages/devices. Never remove the journal to fix a reporting error: that can lose recovery information.

The command is parsed as executable plus quoted arguments, with the artifact path appended. It does not implicitly invoke a shell. Optional recipe `install` commands use `/bin/sh` only when explicitly enabled. `{artifact}` is the downloaded file and `{artifacts}` its directory. Supervised `run`/`shutdown` commands (SF-014) and component configuration with defaults, reset/merge, validation and device-local confidential values (recipe 1.2, SF-015) are described in [the operations runbook](docs/OPERATIONS.md#component-configuration-sf-015).

An updater exit code of zero is reported as Applied. This does not establish post-reboot health or A/B rollback. Interrupted work becomes Failed with an inspection message and is not automatically applied again.

## Validation

```bash
# In the development container
cargo test --manifest-path suifleet-edge/Cargo.toml
cargo build --manifest-path suifleet-edge/Cargo.toml
python3 scripts/test_ota.py
sui move test --path contracts
cd dapp && pnpm typecheck && pnpm build
```

The simulator launches the real daemon and checks recipe downloads, direct/group targeting, integrity failures, rejected unsupported recipes/untrusted operators, pagination, safe enrollment, deleted objects, offline catch-up, restart deduplication and durable report retry. Signing and address behavior match an independent Mysten SDK test vector.

See [recorded validation results](docs/VALIDATION.md). The simulator does not validate actual Sui transaction execution, Walrus certification, Yocto cross-compilation, updater hardware behavior or live wallet flows. Those remain release gates.

## Contracts and packaging

Contracts are in `contracts/`; the app is in `dapp/`; the daemon and Yocto layer are in `suifleet-edge/`. The new `create_deployment_verified` and `report_status_for_group` functions are required for authenticated recipe creation and group reports. Records created through the legacy API without a recipe digest are rejected by the daemon. Existing installations need the corresponding contract upgrade/new publish, reissued signer-bound capabilities, and recreated deployments with recipe digests before using the new daemon.

`scripts/redeploy.sh` publishes or upgrades the contracts on the active Sui CLI environment after verifying the chain, account and UpgradeCap. It records each release and generates both client configs ([docs/DEPLOYMENT_MANIFEST.md](docs/DEPLOYMENT_MANIFEST.md)). No package is published automatically by tests. Calls use the latest package ID; types use the package that introduced them (per-type origins in the manifest).

The Yocto layer installs the daemon and systemd unit. Add your updater separately. The recipe has not been validated by an actual BitBake build; source pinning and cargo dependency integration remain necessary before a production image.

## Agent collaboration and tracking

Read [AGENTS.md](AGENTS.md) or [CLAUDE.md](CLAUDE.md) before working. Only Codex creates new agent tickets/issues unless the user explicitly authorizes another model. Infrastructure, build/test and deployment procedures are in [OPERATIONS.md](docs/OPERATIONS.md); issue ownership and model handoffs are in [ISSUE_TRACKING.md](docs/ISSUE_TRACKING.md).
