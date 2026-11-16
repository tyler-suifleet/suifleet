# Install the SuiFleet device client

Choose an installation path, connect a device and send a harmless first application update. Registration alone does not install or start the client.

## Prerequisites

You need an operator account, network funds, administrative access to the target and a verified client bundle or reviewed source checkout. Use the same network in the console and device configuration. No public prebuilt client, signed APT repository or universally validated Pi image download is offered here.

## Steps

1. Choose your platform in [device client setup](INSTALL_DEVICE_CLIENT.md#get-the-client): Yocto, macOS, Debian/Ubuntu or manual systemd Linux. Verify supplied bundles before installation.
2. Generate the device identity on the target. Never include keys, enrollment credentials or journals in reusable images.
3. In the console, select **Download connection settings**. It saves `deployment.json`, not a complete device configuration.
4. [Register by address](INSTALL_DEVICE_CLIENT.md#register-by-address) or use [Enrollment](CUSTOMER_ENROLLMENT.md). Record the resulting Device and DeviceCap IDs; write the cap ID into Linux `signer.toml` and both IDs into `config.toml`.
5. Install `deployment.json`, then Linux `signer.toml`, then `config.toml` last. Run the configuration check and service checks in the setup guide.
6. [Publish and deploy a component](CUSTOMER_COMPONENTS.md) for the device's OS and architecture. Start with harmless application content.

## Expected result

The configuration check prints `Verified deployment configuration`. The service runs and the console receives a device report. Inspect the actual deployment result; registration or a transaction digest alone does not prove an update succeeded.

## Recovery and limits

For an offline device, check the network, manifest, IDs, signer and report funding before retrying. Preserve `/etc/suifleet/device.key` and `/var/lib/suifleet`. Platform tools own OS installation, reboot and firmware recovery. Physical Pi, manual real-wallet, production-provider and mainnet acceptance remain unrun in this guide.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](INSTALL.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
