# Published SuiFleet issue index

Project: [SuiFleet — OTA and Fleet Management](https://github.com/users/tyler-suifleet/projects/1). Initial seed: 28 issues; stable SF keys are not GitHub issue numbers. Use live Project ownership/status to coordinate models.

| Key | GitHub issue | Priority | Area | Dependencies |
|---|---|---|---|---|
| SF-001 | [#1 Prevent old Sui package entry points from bypassing upgraded authority](https://github.com/tyler-suifleet/suifleet/issues/1) | P0 | security | None |
| SF-002 | [#2 Introduce a network deployment manifest with per-type origins](https://github.com/tyler-suifleet/suifleet/issues/2) | P0 | clients | None |
| SF-003 | [#6 Migrate the frontend SDK and wallet integration to supported Sui APIs](https://github.com/tyler-suifleet/suifleet/issues/6) | P0 | frontend | [SF-002](https://github.com/tyler-suifleet/suifleet/issues/2) |
| SF-004 | [#7 Migrate the daemon reader, transaction builder and executor to supported Sui APIs](https://github.com/tyler-suifleet/suifleet/issues/7) | P0 | edge | [SF-002](https://github.com/tyler-suifleet/suifleet/issues/2) |
| SF-005 | [#8 Generate and verify shared contract, event and recipe interfaces](https://github.com/tyler-suifleet/suifleet/issues/8) | P0 | clients | [SF-002](https://github.com/tyler-suifleet/suifleet/issues/2) |
| SF-006 | [#9 Harden publishing and configuration generation](https://github.com/tyler-suifleet/suifleet/issues/9) | P0 | infra | [SF-002](https://github.com/tyler-suifleet/suifleet/issues/2) |
| SF-007 | [#3 Add reproducible CI and release compatibility gates](https://github.com/tyler-suifleet/suifleet/issues/3) | P0 | infra | None |
| SF-008 | [#10 Implement revocable device identity and capability lifecycle](https://github.com/tyler-suifleet/suifleet/issues/10) | P0 | security | [SF-001](https://github.com/tyler-suifleet/suifleet/issues/1), [SF-002](https://github.com/tyler-suifleet/suifleet/issues/2) |
| SF-009 | [#4 Constrain updater and component execution privileges](https://github.com/tyler-suifleet/suifleet/issues/4) | P0 | security | None |
| SF-010 | [#11 Establish live wallet-to-device and package-upgrade acceptance suite](https://github.com/tyler-suifleet/suifleet/issues/11) | P0 | clients | [SF-001](https://github.com/tyler-suifleet/suifleet/issues/1), [SF-003](https://github.com/tyler-suifleet/suifleet/issues/6), [SF-004](https://github.com/tyler-suifleet/suifleet/issues/7), [SF-005](https://github.com/tyler-suifleet/suifleet/issues/8), [SF-006](https://github.com/tyler-suifleet/suifleet/issues/9), [SF-007](https://github.com/tyler-suifleet/suifleet/issues/3), [SF-008](https://github.com/tyler-suifleet/suifleet/issues/10) |
| SF-011 | [#12 Create immutable component version catalog and platform manifests](https://github.com/tyler-suifleet/suifleet/issues/12) | P1 | runtime | [SF-005](https://github.com/tyler-suifleet/suifleet/issues/8) |
| SF-012 | [#17 Resolve component dependency graphs and version conflicts](https://github.com/tyler-suifleet/suifleet/issues/17) | P1 | runtime | [SF-011](https://github.com/tyler-suifleet/suifleet/issues/12) |
| SF-013 | [#13 Reconcile desired deployment revisions and live group membership](https://github.com/tyler-suifleet/suifleet/issues/13) | P1 | fleet | [SF-002](https://github.com/tyler-suifleet/suifleet/issues/2), [SF-005](https://github.com/tyler-suifleet/suifleet/issues/8) |
| SF-014 | [#19 Supervise component install, run, shutdown, bootstrap and recovery](https://github.com/tyler-suifleet/suifleet/issues/19) | P1 | runtime | [SF-009](https://github.com/tyler-suifleet/suifleet/issues/4), [SF-011](https://github.com/tyler-suifleet/suifleet/issues/12), [SF-012](https://github.com/tyler-suifleet/suifleet/issues/17) |
| SF-015 | [#20 Manage component configuration defaults, merge, reset and validation](https://github.com/tyler-suifleet/suifleet/issues/20) | P1 | runtime | [SF-005](https://github.com/tyler-suifleet/suifleet/issues/8), [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19) |
| SF-016 | [#21 Add health-based application and reboot-aware firmware rollback](https://github.com/tyler-suifleet/suifleet/issues/21) | P1 | edge | [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13), [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19) |
| SF-017 | [#23 Coordinate canary cohorts, rollout rates and failure-threshold abort](https://github.com/tyler-suifleet/suifleet/issues/23) | P1 | fleet | [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13), [SF-019](https://github.com/tyler-suifleet/suifleet/issues/22), [SF-024](https://github.com/tyler-suifleet/suifleet/issues/15) |
| SF-018 | [#18 Implement deployment pause, cancel, expiry and maintenance windows](https://github.com/tyler-suifleet/suifleet/issues/18) | P1 | fleet | [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13) |
| SF-019 | [#22 Report heartbeat, installed inventory and device/component health](https://github.com/tyler-suifleet/suifleet/issues/22) | P1 | fleet | [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19) |
| SF-020 | [#24 Collect remote logs, diagnostics and operational alerts](https://github.com/tyler-suifleet/suifleet/issues/24) | P1 | fleet | [SF-019](https://github.com/tyler-suifleet/suifleet/issues/22), [SF-024](https://github.com/tyler-suifleet/suifleet/issues/15) |
| SF-021 | [#14 Provision fleets with scoped enrollment, roles and hardware signer support](https://github.com/tyler-suifleet/suifleet/issues/14) | P1 | security | [SF-008](https://github.com/tyler-suifleet/suifleet/issues/10) |
| SF-022 | [#25 Distribute confidential artifacts, secrets and rotated credentials](https://github.com/tyler-suifleet/suifleet/issues/25) | P1 | security | [SF-008](https://github.com/tyler-suifleet/suifleet/issues/10), [SF-015](https://github.com/tyler-suifleet/suifleet/issues/20), [SF-021](https://github.com/tyler-suifleet/suifleet/issues/14) |
| SF-023 | [#5 Manage artifact retention, resumable download, cache quotas and budgets](https://github.com/tyler-suifleet/suifleet/issues/5) | P1 | storage | None |
| SF-024 | [#15 Build a scalable fleet indexer, versioned API and operations CLI](https://github.com/tyler-suifleet/suifleet/issues/15) | P1 | fleet | [SF-002](https://github.com/tyler-suifleet/suifleet/issues/2), [SF-005](https://github.com/tyler-suifleet/suifleet/issues/8) |
| SF-025 | [#16 Validate and pin Yocto image and first-boot service packaging](https://github.com/tyler-suifleet/suifleet/issues/16) | P1 | infra | [SF-004](https://github.com/tyler-suifleet/suifleet/issues/7), [SF-009](https://github.com/tyler-suifleet/suifleet/issues/4) |
| SF-026 | [#26 Update the daemon safely with bootstrap handoff and fallback](https://github.com/tyler-suifleet/suifleet/issues/26) | P1 | edge | [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19), [SF-016](https://github.com/tyler-suifleet/suifleet/issues/21), [SF-025](https://github.com/tyler-suifleet/suifleet/issues/16) |
| SF-027 | [#27 Provide authenticated component IPC and developer SDK](https://github.com/tyler-suifleet/suifleet/issues/27) | P2 | compatibility | [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19), [SF-015](https://github.com/tyler-suifleet/suifleet/issues/20), [SF-019](https://github.com/tyler-suifleet/suifleet/issues/22) |
| SF-028 | [#28 Specify and validate broader Greengrass compatibility adapters](https://github.com/tyler-suifleet/suifleet/issues/28) | P2 | compatibility | [SF-027](https://github.com/tyler-suifleet/suifleet/issues/27) |

Only Codex creates new agent tickets/issues unless the user explicitly authorizes another model. All models may work on explicitly assigned existing issues under the shared instructions.

Saved views:

- [Workflow](https://github.com/users/tyler-suifleet/projects/1/views/2)
- [All work](https://github.com/users/tyler-suifleet/projects/1/views/3)
- [P0 release blockers](https://github.com/users/tyler-suifleet/projects/1/views/4)
- [Codex work](https://github.com/users/tyler-suifleet/projects/1/views/5)
- [Claude work](https://github.com/users/tyler-suifleet/projects/1/views/6)
- [Unassigned work](https://github.com/users/tyler-suifleet/projects/1/views/7)
