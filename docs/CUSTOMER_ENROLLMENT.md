# Enroll devices and prepare provisioning media

Use Enrollment to reserve device names and give each target a single-use bootstrap credential, without copying device addresses into the console.

## Prerequisites

Connect the fleet operator account on the intended network. Install the client and provision a unique local device key first. Have funds for claim enrollment sponsorship and separately for ongoing device reports. Treat credential archives as secrets.

## Steps

1. Open **Enrollment**. Create or select a batch policy with sufficient quota and an appropriate expiry.
2. Use **Import CSV** (`serial` required; `alias` or `name`, and `group` optional), **Pair one device**, or **Import roster file**. Resolve duplicate names, serials and ambiguous groups before continuing.
3. Select **Download credentials**. Retain the archive securely: it is the only saved copy of the browser-generated claim keys. Each `enrollment/SERIAL/` folder contains `enroll.toml`, `claim.key` and `deployment.json`.
4. Select **Run pending transactions** to register and fund claims. Approve only the intended network and batch in your wallet. Give each device only its own folder on private provisioning media.
5. On the target, run:

```sh
sudo suifleet --enroll /path/to/enroll.toml
```

6. Read the Device object ID (`device_object_id`) and DeviceCap ID (`device_cap_id`) from successful enrollment output. Put the cap ID in Linux `signer.toml`, and both IDs in `config.toml`; install the manifest, signer file and daemon file in that order. See [setup](INSTALL_DEVICE_CLIENT.md#configuration-file).
7. Run pending transactions again to bind enrolled devices to their reserved aliases, serials and groups. Use **Fund enrolled devices** or your configured device operating budget for ongoing reports.

## Expected result

The roster reaches **Enrolled**, the named device appears in Devices, and its configured client sends reports. **Waiting for device** means the claim is ready, not that the client is installed or running.

## Recovery and limits

Refresh after an interrupted transaction; the page reconciles confirmed work. Resolve **Conflict** or **Not yours** instead of changing credentials blindly. Never re-register a withdrawn claim without investigating the earlier submission. Revoke leaked unused claims, use small expiring batches and erase consumed claim media. A leaked unused credential can enroll someone else's device into your fleet. Enrollment sponsorship does not fund all future reports.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](ENROLLMENT.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
