# Greengrass capability-to-ticket coverage

The 51-capability audit is a functional OTA/fleet and broader-runtime roadmap, not proof of complete AWS API or managed-component compatibility. All rows have a tracked owner scope below; [SF-038](https://github.com/tyler-suifleet/suifleet/issues/39) remains a specification epic that must produce separate implementation tickets for selected managed adapters and platform support. AWS-managed components span many service-specific integrations; no finite seed backlog guarantees every future feature is covered.

Reference: [AWS Greengrass V2 overview](https://docs.aws.amazon.com/greengrass/v2/developerguide/what-is-iot-greengrass.html) and [AWS-provided components](https://docs.aws.amazon.com/greengrass/v2/developerguide/public-components.html), checked 2026-10-13.

[GREENGRASS_MANAGED_COMPONENTS.md](GREENGRASS_MANAGED_COMPONENTS.md) (SF-038) maps all 46 AWS-provided components to native equivalents, scoped adapters ([SF-057](https://github.com/tyler-suifleet/suifleet/issues/102) to [SF-063](https://github.com/tyler-suifleet/suifleet/issues/108)) or roadmap gaps ([SF-064](https://github.com/tyler-suifleet/suifleet/issues/109)). None is implemented and no live AWS check has run.

[GREENGRASS_COMPATIBILITY.md](GREENGRASS_COMPATIBILITY.md) (SF-028) defines the functional, AWS-recipe and AWS-API compatibility levels, the supported platforms and the proposed adapter fixtures for SF-032–SF-035 and SF-037. SF-037 has executed the recipe and managed-dependency suites against its translator; the other runtime suites remain pending until their tickets execute them.

P2 means broader compatibility, not optional for a full-parity claim. Each row needs executable acceptance evidence, including failures and recovery, before completion. See [the audit](GREENGRASS_PARITY_AUDIT.md).

| Capability | Tracking keys |
|---|---|
| Individual and static group targeting | [SF-010](https://github.com/tyler-suifleet/suifleet/issues/11), [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32) |
| Artifact distribution and integrity | [SF-010](https://github.com/tyler-suifleet/suifleet/issues/11), [SF-022](https://github.com/tyler-suifleet/suifleet/issues/25), [SF-023](https://github.com/tyler-suifleet/suifleet/issues/5), [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32) |
| Durable offline delivery | [SF-004](https://github.com/tyler-suifleet/suifleet/issues/7), [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13), [SF-023](https://github.com/tyler-suifleet/suifleet/issues/5) |
| Trusted update authorization | [SF-001](https://github.com/tyler-suifleet/suifleet/issues/1), [SF-008](https://github.com/tyler-suifleet/suifleet/issues/10), [SF-021](https://github.com/tyler-suifleet/suifleet/issues/14) |
| Deployment statuses | [SF-018](https://github.com/tyler-suifleet/suifleet/issues/18), [SF-019](https://github.com/tyler-suifleet/suifleet/issues/22), [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32) |
| Desired fleet state and revisions | [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13) |
| Newly joined devices receive desired state | [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13) |
| Multi-component atomic deployments | [SF-012](https://github.com/tyler-suifleet/suifleet/issues/17), [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13), [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19) |
| Staged/canary rollout | [SF-017](https://github.com/tyler-suifleet/suifleet/issues/23) |
| Failure-threshold abort | [SF-017](https://github.com/tyler-suifleet/suifleet/issues/23) |
| Pause/resume/cancel | [SF-018](https://github.com/tyler-suifleet/suifleet/issues/18) |
| Device update timeout | [SF-018](https://github.com/tyler-suifleet/suifleet/issues/18) |
| Maintenance windows/update deferral | [SF-018](https://github.com/tyler-suifleet/suifleet/issues/18) |
| Automatic application rollback | [SF-016](https://github.com/tyler-suifleet/suifleet/issues/21) |
| Platform-owned OS/firmware installer handoff and explicit results | [SF-016](https://github.com/tyler-suifleet/suifleet/issues/21) |
| Safe daemon self-update | [SF-026](https://github.com/tyler-suifleet/suifleet/issues/26) |
| Artifact lifecycle | [SF-023](https://github.com/tyler-suifleet/suifleet/issues/5) |
| Component versions/catalog | [SF-011](https://github.com/tyler-suifleet/suifleet/issues/12) |
| Recipe format | [SF-005](https://github.com/tyler-suifleet/suifleet/issues/8), [SF-037](https://github.com/tyler-suifleet/suifleet/issues/36) |
| Install lifecycle | [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19), [SF-030](https://github.com/tyler-suifleet/suifleet/issues/30) |
| Run/startup/shutdown/bootstrap/recover | [SF-014](https://github.com/tyler-suifleet/suifleet/issues/19) |
| Dependency resolution | [SF-012](https://github.com/tyler-suifleet/suifleet/issues/17) |
| Platform manifests | [SF-011](https://github.com/tyler-suifleet/suifleet/issues/12) |
| Configuration merge/reset/validation | [SF-015](https://github.com/tyler-suifleet/suifleet/issues/20) |
| Runtime isolation | [SF-009](https://github.com/tyler-suifleet/suifleet/issues/4) |
| Local deployment/component CLI | [SF-027](https://github.com/tyler-suifleet/suifleet/issues/27) |
| Container component adapters | [SF-036](https://github.com/tyler-suifleet/suifleet/issues/35) |
| Lambda/AWS-managed component compatibility | [SF-038](https://github.com/tyler-suifleet/suifleet/issues/39), [SF-064](https://github.com/tyler-suifleet/suifleet/issues/109) |
| Inventory and groups | [SF-013](https://github.com/tyler-suifleet/suifleet/issues/13), [SF-019](https://github.com/tyler-suifleet/suifleet/issues/22), [SF-024](https://github.com/tyler-suifleet/suifleet/issues/15) |
| Software key provisioning | [SF-021](https://github.com/tyler-suifleet/suifleet/issues/14), [SF-029](https://github.com/tyler-suifleet/suifleet/issues/29) |
| Hardware identity/TPM/HSM | [SF-021](https://github.com/tyler-suifleet/suifleet/issues/14) |
| Key rotation/revocation | [SF-008](https://github.com/tyler-suifleet/suifleet/issues/10) |
| Online/last-seen/heartbeat | [SF-019](https://github.com/tyler-suifleet/suifleet/issues/22) |
| Installed component and device health | [SF-019](https://github.com/tyler-suifleet/suifleet/issues/22) |
| Diagnostics and remote logs | [SF-020](https://github.com/tyler-suifleet/suifleet/issues/24) |
| Fleet API/CLI and automation | [SF-024](https://github.com/tyler-suifleet/suifleet/issues/15) |
| Operator RBAC and delegation | [SF-021](https://github.com/tyler-suifleet/suifleet/issues/14) |
| Production indexer | [SF-024](https://github.com/tyler-suifleet/suifleet/issues/15) |
| Secrets and confidential configuration | [SF-022](https://github.com/tyler-suifleet/suifleet/issues/25) |
| Cost/network policy | [SF-021](https://github.com/tyler-suifleet/suifleet/issues/14), [SF-023](https://github.com/tyler-suifleet/suifleet/issues/5), [SF-024](https://github.com/tyler-suifleet/suifleet/issues/15) |
| Packaging/provisioning lifecycle | [SF-025](https://github.com/tyler-suifleet/suifleet/issues/16), [SF-029](https://github.com/tyler-suifleet/suifleet/issues/29) |
| Publish/upgrade tooling | [SF-006](https://github.com/tyler-suifleet/suifleet/issues/9) |
| Production release pipeline | [SF-007](https://github.com/tyler-suifleet/suifleet/issues/3), [SF-039](https://github.com/tyler-suifleet/suifleet/issues/31) |
| Component IPC and SDK | [SF-027](https://github.com/tyler-suifleet/suifleet/issues/27) |
| Local pub/sub and MQTT bridge | [SF-032](https://github.com/tyler-suifleet/suifleet/issues/33) |
| Device shadows | [SF-033](https://github.com/tyler-suifleet/suifleet/issues/37) |
| Stream manager/store-and-forward | [SF-034](https://github.com/tyler-suifleet/suifleet/issues/34) |
| Client-device discovery/authentication | [SF-035](https://github.com/tyler-suifleet/suifleet/issues/38) |
| Token exchange/cloud credentials | [SF-022](https://github.com/tyler-suifleet/suifleet/issues/25), [SF-038](https://github.com/tyler-suifleet/suifleet/issues/39), [SF-057](https://github.com/tyler-suifleet/suifleet/issues/102) |
| ML inference/industrial/cloud integrations | [SF-038](https://github.com/tyler-suifleet/suifleet/issues/39), [SF-058](https://github.com/tyler-suifleet/suifleet/issues/103) to [SF-063](https://github.com/tyler-suifleet/suifleet/issues/108), [SF-064](https://github.com/tyler-suifleet/suifleet/issues/109) |
| Windows/macOS runtime | [SF-064](https://github.com/tyler-suifleet/suifleet/issues/109) (roadmap gap; SF-038 defines the initial Linux phase) |

## Hardware acceptance

[SF-029](https://github.com/tyler-suifleet/suifleet/issues/29) builds a bootable Yocto image for the Pi 3 B+ with the SF-025 BitBake client package; [SF-030](https://github.com/tyler-suifleet/suifleet/issues/30) supplies versioned harmless application fixtures; [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32) exercises the real frontend wallet, Sui/Walrus and device. [SF-010](https://github.com/tyler-suifleet/suifleet/issues/11) retains upgrade/migration acceptance. [SF-016](https://github.com/tyler-suifleet/suifleet/issues/21) owns SuiFleet installer handoff and journal/result semantics; platform scripts/tools own installation, reboot and firmware recovery. Physical recovery evidence validates the selected platform integration, not a universal A/B requirement. Extend the Pi acceptance matrix as each new capability lands; a one-shot OTA smoke test cannot establish complete runtime parity.
