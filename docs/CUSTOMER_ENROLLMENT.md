# Enroll devices and prepare provisioning media

Use Enrollment to reserve device names and give each target a single-use bootstrap credential, without copying device addresses into the console.

## Prerequisites

Connect the fleet operator account on the intended network. Install a client with the first-boot enrollment unit and prepare the root filesystem with the public batch settings. The client provisions its own unique key at boot. Have funds for claim enrollment sponsorship and separately for ongoing device reports. Treat credential archives as secrets.

For already-flashed cards and USB volumes, see [Write one claim per medium](#write-one-claim-per-medium).

## Steps

1. Open **Enrollment**. Create or select a batch policy with sufficient quota and an appropriate expiry.
2. Use **Import CSV** (`serial` required; `alias` or `name`, and `group` optional), **Pair one device**, or **Import roster file**. Resolve duplicate names, serials and ambiguous groups before continuing.
3. Select **Download credentials**. If the batch uses an updater, reporting, logs or archives, paste its TOML configuration in **Batch configuration template**. Keep the archive offline. Each `enrollment/SERIAL/` folder contains `enroll.toml`, `claim.key`, `deployment.json` and, when supplied, `config.template.toml`.
4. Select **Run pending transactions** to register and fund claims. Approve the intended network and batch in your wallet.
5. Install the same [public trust settings](ENROLLMENT.md#automatic-linux-first-boot) in the root filesystem for the batch. Put exactly one `enrollment/SERIAL/` folder on each unit's private writable boot partition or configured provisioning media.
6. Boot the device. First-boot enrollment verifies the inputs, consumes the claim, writes the client configuration and starts the client. Check `enrollment-status.json` in the unit's folder. The same status code goes to the service journal. If you supplied no updater template, the default updater refuses artifacts until you configure an updater.
7. Run pending transactions again to bind enrolled devices to their reserved aliases, serials and groups. Use **Fund enrolled devices** or your configured device operating budget for ongoing reports.

## Write one claim per medium

On Linux or macOS with Python 3.11 or later, flash the golden image using your
normal process, then mount the intended card's FAT boot partition or a private
FAT/exFAT USB volume. The host tool writes files on an existing filesystem.
Keep the archive and assignment database in a private directory on the host.
Use one database for every station writing from the same credentials. If several
stations would use separate databases, route those writes through one host
instead. Keep the database through interruptions and retain it until all its
claims are consumed, expired or revoked.

Identify the mounted partition with `lsblk -o NAME,RM,FSTYPE,SIZE,MOUNTPOINTS`
on Linux or `diskutil list external physical` and `diskutil info` on macOS. Check
it against the card or stick you just inserted. Use the exact mounted volume
root. The host must identify the disk as removable. If a reader reports the
medium as fixed, use a removable reader instead. On Windows, use a Linux or
macOS provisioning station and attach the media directly to that host.

From the SuiFleet source checkout, substitute your archive and volume paths:

```sh
python3 scripts/provision_enrollment_media.py --log "$HOME/private/assignments.db" \
  write --archive "$HOME/private/credentials.tar" --serial unit-001 \
  --target "/media/operator/BOOT"
```

For an unattended batch, replace `--serial unit-001` with `--next` and pass one
`--target` for each mounted volume, in station order. Serial selection is
alphabetical, ignoring case. The command stops at the first error and prints
the serial and media ID for each completed write. Label the media with its
serial, eject it safely, and keep it with that unit. Flash golden images before
assignment and never clone provisioned media or copy the generated media ID.

After interruption, remount the same medium and repeat the command with the
same archive and database. The tool resumes a pending write for that medium or
verifies an already completed one. A missing or damaged medium can still hold
the claim. Revoke its unused claim and generate a fresh credential before
provisioning the replacement.

For returned media, use `scrub` with its recorded serial and the same database:

```sh
python3 scripts/provision_enrollment_media.py --log "$HOME/private/assignments.db" \
  scrub --serial unit-001 --target "/media/operator/BOOT"
```

Scrub overwrites, flushes and removes the live claim file, including a claim
left in staging, and retires the assignment. Repeating it is safe. Revoke any
unused claim, and sanitize or retire the medium before reuse. Flash wear
leveling, snapshots and archive copies can preserve the original bytes.

## Expected result

First-boot enrollment records both the device object ID and the `DeviceCap` ID in `config.toml` and configures `signer.toml` with the `DeviceCap` ID. The roster reaches **Bound**, the named device appears in Devices, and its configured client sends reports. **Funded, unused** means the claim is ready. **Consumed, pending binding** means the chain recorded enrollment and the reservation still needs to be bound.

## Recovery and limits

Refresh after an interrupted transaction; the page reconciles confirmed work. Resolve **Conflict** or **Not yours** instead of changing credentials blindly. Never re-register a withdrawn claim without investigating the earlier submission. Revoke leaked unused claims, use small expiring batches and sanitize or retire the media after provisioning. A leaked unused credential can enroll someone else's device into your fleet. Enrollment sponsorship does not fund all future reports.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](ENROLLMENT.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
