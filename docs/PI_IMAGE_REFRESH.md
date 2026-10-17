# SF-066 image refresh: blocked build attempt

The refresh is incomplete. No current-source SD image, checksum, package
manifest, rootfs inspection or packaged-binary compatibility result is claimed.
The existing [SF-029 image](PI_IMAGE_BUILD.md) remains an older public smoke
baseline; its hashes do not describe this release.

Reviewed source pin: `a80784b97f41492558ad33bcb753a0a20505a3a0`, the merged
SF-053 release containing SF-026 launcher support. Cargo.lock SHA-256 remains
`44aa5b2637fc2b828a796a63e9a550d9c0fa652437cf59ffd31e0b264021290e`.
`bitbake -c update_crates suifleet` exited 0: 336 tasks, 332 reused.
The generated include was byte-identical. Independent comparison of the actual
unpacked source HEAD, lock digest and all 302 crate URIs/checksums passed.

The owned build copies the SF-029 pinned Scarthgap/Rust-mixin configuration,
points the SuiFleet layer to the SF-066 worktree and retains the private
public-key-only access overlay. External layer revisions printed in the build
log match `kas/packaging.yml`. No existing image/build tree was modified.

Exact commands, with `umask 022`:

```sh
source /home/tyler/dev/suifleet-yocto/poky/oe-init-build-env /home/tyler/dev/codex/yocto-sf066/build
bitbake -c update_crates suifleet
bitbake suifleet-pi-image
```

The image build exited 1. The disk monitor reported STOPTASKS at 1.787 GB
free and HALT at 0.866 GB, terminating the two remaining tasks. Summary:
1331 tasks attempted, 1142 reused; no recipe-task failure before the disk halt.
The attempt began with approximately 15 GiB available. No disk/QA check was
bypassed. Logs and configuration remain in `/home/tyler/dev/codex/yocto-sf066/`.
After confirming all tasks and the BitBake server stopped, only this attempt's
generated `build/tmp-systemd` was removed to restore host space. Shared caches,
previous build trees and historical artifacts were preserved. The build lease
was released; no hardware/network lease was taken.

Offline verification:

```sh
python3 -m unittest scripts.tests.test_inspect_pi_image scripts.tests.test_test_component
python3 -m py_compile scripts/inspect_pi_image.py scripts/test_pi_binary.py
git diff --check
```

The 19 offline tests passed; syntax and whitespace checks passed. New
image-inspection and packaged-binary cases require the actual new image and
remain unrun. The inspector now requires separate signer accounts, correct
binary/helper/configuration ownership and permissions, signer activation,
launcher/recorder wiring and absent runtime credentials. The binary runner
checks offline mixed-origin manifest/client compatibility, key-holder selection
and refusal of self-update with the service signer.

Provide sufficient free build storage, then rerun the real SD-image target.
Inspect its WIC partitions/rootfs, services, users, permissions, credentials,
public-key SSH/DHCP configuration, source/build-info and binary hashes. Run the
packaged AArch64 compatibility/identity checks, publish new checksums,
manifest and complete provenance, and obtain current-head CI before Review.
The older image or an SSH binary copy cannot establish this acceptance.

The reviewed security boundary rejects enabled daemon self-update with the
service signer; this refresh preserves that restriction. Reusable session
credentials are not provisioned. Physical flashing, boot, network, persistence
and recovery remain [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
No device programming, live-chain mutation or production deployment occurred.
