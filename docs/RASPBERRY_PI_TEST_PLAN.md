# Raspberry Pi end-to-end acceptance plan

Implementation is tracked in SF-029 (bootable Yocto image), SF-030 (versioned test app), and SF-031 (live acceptance). These tickets specify work; no Pi deployment or live testnet run has happened yet.

## Target configuration

Confirmed hardware: Raspberry Pi 3 Model B+, with no confirmed installed image. Build a 64-bit Yocto image using the meta-raspberrypi `raspberrypi3-64` machine and compatible pinned layers/toolchain. The layer supports pinned Scarthgap with the Rust mixin and Wrynose profiles; their recorded emulation results do not establish physical Pi acceptance. Reference: [Scarthgap Raspberry Pi BSP machines](https://layers.openembedded.org/layerindex/branch/scarthgap/layer/meta-raspberrypi/).

Take `ticket --project suifleet lock hardware:raspberrypi3bplus` only for the duration of flashing, booting or on-device tests and unlock immediately afterwards (see [ATOMIC_TICKETING.md](ATOMIC_TICKETING.md#hardware-and-other-shared-resources)); tickets that need the Pi never block claims. Use wired Ethernet for initial boot. SSH host/user and verified host keys become relevant after imaging; serial diagnostics are an alternative. Keep credentials and test funding configuration outside Git. Never bake a private device key into the image.

SF-025 must make the client BitBake recipe reproducible, package the Cargo.lock dependency graph, and install its binary/service/configuration support. SF-029 must supply a pinned build configuration and bootable SD-card test image that includes that package. The build runs on a capable development host; the Pi runs the resulting image. First boot generates a unique identity and exposes its address in an unenrolled state. After registration supplies device/capability IDs and a verified network manifest, enable the enrolled daemon. Preserve keys and journals across restart. An SSH-copied binary cannot substitute for proving image integration.

SF-025's recipe pins source/crates and separates first-boot identity generation from enrollment. Supported profiles cover Scarthgap plus the Rust mixin and Wrynose; pinned packaging and emulation profiles and enrollment instructions are in [YOCTO_PACKAGING.md](YOCTO_PACKAGING.md). The image installs only configuration examples, so install a verified manifest/config bundle after registering the newly generated device address. Packaging/emulation evidence is separate from this physical Pi image and live test loop.

## Live acceptance flow

1. Build the client package and Pi image through BitBake. Record layer/source revisions, image checksum and package manifest. Flash an explicitly selected SD card, boot the physical Pi, and verify architecture, packaged binary hash and unique first-boot device address. Flashing is separate from the build and must not target a guessed disk.
2. Load a verified manifest for the selected local Sui network or testnet. Validate chain identity, per-type origins, current call IDs, endpoint compatibility and test-account funding. Supported-transport work remains a blocker for a reliable public-network run.
3. Register the Pi address and issue its capability through the actual frontend wallet flow. Verify transaction effects and configure the device IDs without exporting its private key.
4. Build the harmless v1/v2 test app with a fresh run ID, using `scripts/test_component.py` ([component README](../tests/components/suifleet-test-app/README.md)). Include `suifleet-test-component` (which provides `/usr/bin/suifleet-test-updater`) in the test image and set it as `apply.command`. Upload each artifact and recipe to compatible Walrus storage, and bind exact hashes in the deployment transaction. Read `/var/lib/suifleet/test-components/suifleet-test-app/receipt.json` and `history.jsonl` for the version/run-ID receipts; the `arch` should be `aarch64`.
5. Start the enrolled client before creating the deployment; first enrollment intentionally establishes a baseline that skips older events. Deploy v1 to the device and v2 through its group. Match frontend status, on-chain effects/status, artifact hash and a version/run-ID receipt written by the Pi application. An exit code alone is insufficient.
6. Exercise offline delivery, reconnect, daemon restart, reporting failure/retry, unauthorized targets, tampered recipes/artifacts and updater failure. Verify rejected work did not execute and failed work is not reported Applied.
7. Collect redacted evidence with tool versions, transaction/blob IDs, binary/payload hashes and test results. Preserve keys and journals; clean up only test-owned resources.

Manual wallet acceptance and unattended test-account automation are separate modes. Automated account transactions do not establish that the browser wallet works. Testnet accounts and funding must be explicitly selected; local profiles provide a repeatable alternative to testnet resets and storage expiry.

## Growing coverage

The initial test app uses the supported one-shot updater and writes only to its isolated test directory. Add supervised run/stop/readiness, configuration, dependency resolution, health telemetry, cancellation, rollback and daemon self-update cases as their implementations land. Track every case as passed, failed, skipped or blocked. Platform scripts/tools own OS installation, reboot, boot confirmation and firmware rollback. SF-016 validates configured installer handoff and SuiFleet journal/result behavior across platform-controlled restarts; selected-platform physical recovery evidence is separate and cannot be inferred from a harmless application update.
