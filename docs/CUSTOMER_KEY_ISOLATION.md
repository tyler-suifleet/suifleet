# Configure the Linux signing service

Keep the device signing key outside the daemon and its component processes using the packaged separate signing service.

## Prerequisites

Use the shipped systemd units, supported Linux kernel and separate `suifleet` and `suifleet-signer` users. Have a verified network manifest and the registered DeviceCap ID. This service boundary is not a hardware-keystore claim.

## Steps

1. Provision a unique device key at runtime; never copy it from another device or into an image.
2. Install `deployment.json` first, then prepare `/etc/suifleet/signer.toml` from the packaged example. Match its manifest and DeviceCap ID to the daemon configuration.
3. Install the signer file root-owned, group `suifleet-signer`, mode 0640. Follow [the exact setup example](INSTALL_DEVICE_CLIENT.md#signing-service-configuration-signertoml).
4. Configure `[device.signer]` with `kind = "service"` and `/run/suifleet-signer/sign.sock`. Install daemon configuration last.
5. Check status and logs:

```sh
systemctl status suifleet-signer.socket suifleet-signer.service suifleet.service
journalctl -u suifleet-signer.service -u suifleet.service
```

## Expected result

The daemon obtains policy-checked signatures without reading the private key. The signer checks the permitted daemon caller and intended device/report operation.

## Recovery and limits

For startup failures, check manifest/cap agreement, file permissions and unit configuration. Preserve the key and state. Migration of existing daemon-owned keys requires stopping services and following the technical migration procedure; never generate a replacement just to fix permissions. Root and the signer remain trusted. PKCS#11 provisioning and physical hardware protection require separate acceptance; macOS uses a different direct-key boundary.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](KEY_ISOLATION.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
