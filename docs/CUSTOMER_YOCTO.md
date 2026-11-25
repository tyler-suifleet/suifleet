# Build a Yocto client image

Integrate the daemon, isolated signer and runtime provisioning into your own Yocto image.

## Prerequisites

Use a reviewed source checkout, Ubuntu 22.04 and kas 4.7 with the pinned profiles. Obtain build resources and storage for BitBake. No standalone public layer repository or Layer Index listing is offered yet; use the layer in this checkout.

## Steps

1. Keep build work outside the checkout and select the Scarthgap recipe profile:

```sh
export KAS_WORK_DIR=/absolute/path/to/yocto-work
export KAS_BUILD_DIR="$KAS_WORK_DIR/build-packaging"
kas build kas/packaging.yml
```

2. For Wrynose use `kas/packaging-wrynose.yml`, and for Kirkstone `kas/packaging-kirkstone.yml`, each in separate work/build trees. Kirkstone kernels lack the signer's strong caller binding and Dunfell is unsupported; see [older Yocto series](YOCTO_LEGACY_SERIES.md). Preserve the pinned daemon source revision; source updates must regenerate the crate include and lock digest together.
3. For a custom image, add `suifleet-edge/meta-suifleet` and the `suifleet` package. Pi image/test integration also needs `suifleet-edge/meta-suifleet-integration`; the committed kas profiles include it.
4. Inspect the package manifest and root filesystem. Builds must contain no device keys, enrollment credentials or journals.
5. At first boot provision a new key, then complete [device setup](INSTALL_DEVICE_CLIENT.md). Supply the manifest, Linux signer configuration and daemon configuration in that order.

## Expected result

BitBake produces packages for the selected profile. The provisioning service creates a target-local identity; the daemon waits for runtime configuration.

## Recovery and limits

For missing dependencies, reproduce with the pinned layers rather than substituting arbitrary revisions. Keep each series' TMPDIR separate. QEMU test profiles allow passwordless console login and are disposable validation only. A package or QEMU boot is not a physical Pi, power-loss or firmware recovery result.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](YOCTO_PACKAGING.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
