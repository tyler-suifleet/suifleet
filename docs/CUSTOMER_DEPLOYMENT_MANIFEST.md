# Use connection settings safely

Use the console's network manifest to connect a device to the same chain and contract deployment. It contains public identifiers, not device secrets.

## Prerequisites

Use the intended fleet console/network and a compatible client. Confirm the operator's deployment provenance before installing new connection settings.

## Steps

1. In device setup, expand **Manual setup for device integrators** if needed and select **Download connection settings**. The downloaded filename is `deployment.json`.
2. Install it at `/etc/suifleet/deployment.json`. On Linux use root ownership, group `suifleet`, mode 0640. Do not edit IDs by guesswork.
3. Reference it as `manifest_path = "deployment.json"` in `[sui]` of `config.toml` and in Linux `signer.toml`.
4. Complete [the Device/DeviceCap ID handoff](INSTALL_DEVICE_CLIENT.md#register-by-address); those IDs are not populated by the network-manifest download.
5. Run `sudo suifleet --check-config /etc/suifleet/config.toml` before normal operation.

## Expected result

The client verifies chain identity, supported protocol/client range, package and shared-object identities. Successful checking prints `Verified deployment configuration`.

## Recovery and limits

If verification fails, obtain the current manifest and compatible client from the operator. Preserve keys and journals. Never replace all type origins with the latest package ID: existing types retain their introduction origins. Older package versions remain callable; a new manifest alone does not disable them or migrate objects.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](DEPLOYMENT_MANIFEST.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
