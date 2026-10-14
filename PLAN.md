# SuiFleet delivery plan

Updated 2026-10-13. The original scaffold checklist was stale. The feature inventory, evidence and release gates are in [the Greengrass parity audit](docs/GREENGRASS_PARITY_AUDIT.md). Work is tracked in [the GitHub Project](https://github.com/users/tyler-suifleet/projects/1) and [published issue index](docs/tracking/INDEX.md); assignment and status are maintained on GitHub.

## Implemented foundation

- [x] Move devices, capabilities, shared groups and deployment records.
- [x] Wallet UI for inventory, groups, uploads and device/group deployment.
- [x] Recipe-to-artifact OTA with on-chain recipe/artifact digests and bounded apply.
- [x] Correct Sui software identity and transaction signing.
- [x] Current operator/group targeting and on-chain status target checks.
- [x] Durable cursor/outcome journal, restart deduplication and status retry.
- [x] Software key provisioning and address CLI.
- [x] Per-device status UI and paginated inventory/history.
- [x] Unit tests and reproducible local chain/storage simulator.

## Required for release

- [ ] Migrate legacy JSON-RPC/wallet integration to supported Sui APIs.
- [ ] Add capability revocation and least-privilege execution.
- [ ] Verify the repaired flow on testnet and real update hardware.
- [ ] Validate and pin Yocto packaging and artifact/storage lifecycle.

## Required for OTA/fleet feature parity

- [ ] Component catalog, semver/dependencies/platform manifests.
- [ ] Desired-state revisions, membership reconciliation and multi-component transactions.
- [ ] Component lifecycle supervisor, configuration management and local CLI.
- [ ] Application rollback, health validation, reboot-aware A/B firmware and daemon self-update.
- [ ] Canary/rate-limited rollout, abort gates, pause/cancel/expiry and maintenance windows.
- [ ] Heartbeats, installed inventory, fleet health, logs, diagnostics and alerts.
- [ ] Fleet provisioning, rotation/revocation, delegated roles and confidential secrets.
- [ ] Scalable indexer/coordinator, fleet API/CLI, automation and funding/retention policy.
- [ ] CI, reproducible signed releases and failure/power-loss acceptance suite.

## Required for broader Greengrass equivalence

- [ ] Component IPC/SDK, local pub/sub and MQTT bridge.
- [ ] Shadows, durable streams, client-device discovery/authentication.
- [ ] Cloud credential/adaptor services and selected managed-component compatibility.
- [ ] Explicit platform support and recipe/API compatibility contracts.
