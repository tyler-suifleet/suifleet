# Enroll devices and prepare provisioning media

Use Enrollment to reserve device names and give each target a single-use bootstrap credential, without copying device addresses into the console.

## Prerequisites

Connect the fleet operator account on the intended network. Install a client with the first-boot enrollment unit and prepare the root filesystem with the public batch settings. The client provisions its own unique key at boot. Have funds for claim enrollment sponsorship and separately for ongoing device reports. Treat credential archives as secrets.

## Steps

1. Open **Enrollment**. Create or select a batch policy with sufficient quota and an appropriate expiry.
2. Use **Import CSV** (`serial` required; `alias` or `name`, and `group` optional), **Pair one device**, or **Import roster file**. Resolve duplicate names, serials and ambiguous groups before continuing.
3. Select **Download credentials**. If the batch uses an updater, reporting, logs or archives, paste its TOML configuration in **Batch configuration template**. Keep the archive offline. Each `enrollment/SERIAL/` folder contains `enroll.toml`, `claim.key`, `deployment.json` and, when supplied, `config.template.toml`.
4. Select **Run pending transactions** to register and fund claims. Approve the intended network and batch in your wallet.
5. Install the same [public trust settings](ENROLLMENT.md#automatic-linux-first-boot) in the root filesystem for the batch. Put exactly one `enrollment/SERIAL/` folder on each unit's private writable boot partition or configured provisioning media.
6. Boot the device. First-boot enrollment verifies the inputs, consumes the claim, writes the client configuration and starts the client. Check `enrollment-status.json` in the unit's folder. The same status code goes to the service journal. If you supplied no updater template, the default updater refuses artifacts until you configure an updater.
7. Run pending transactions again to bind enrolled devices to their reserved aliases, serials and groups. Use **Fund enrolled devices** or your configured device operating budget for ongoing reports.

## Expected result

First-boot enrollment records both the device object ID and the `DeviceCap` ID in `config.toml` and configures `signer.toml` with the `DeviceCap` ID. The roster reaches **Bound**, the named device appears in Devices, and its configured client sends reports. **Funded, unused** means the claim is ready. **Consumed, pending binding** means the chain recorded enrollment and the reservation still needs to be bound.

## Recovery and limits

Refresh after an interrupted transaction; the page reconciles confirmed work. Resolve **Conflict** or **Not yours** instead of changing credentials blindly. Never re-register a withdrawn claim without investigating the earlier submission. Revoke leaked unused claims, use small expiring batches and sanitize or retire the media after provisioning. A leaked unused credential can enroll someone else's device into your fleet. Enrollment sponsorship does not fund all future reports.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](ENROLLMENT.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
