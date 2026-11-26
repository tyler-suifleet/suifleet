# Fleet enrollment, deployer roles and device signers (SF-021, SF-100)

This builds on SF-008's identities:
- Activated devices live in registry custody.
- The device's operator is whoever owns its `DeviceOwnerCap`.
- Devices apply only their operator's deployments.

## Claim enrollment and bulk onboarding

**Goal.** Devices join an operator's fleet themselves, in bulk, without SUI of their own.

**Setup (operator, once per batch):**

```bash
# 1. A policy: quota 500, no expiry (or an expiry as clock milliseconds).
sui client ptb --move-call PKG::enrollment::create_policy @REGISTRY 500 0 --gas-budget 50000000
# 2. Claim keys, generated offline, one per device's provisioning media.
scripts/claims.py generate 100 /secure/claims-2026-10
# 3. One transaction adds every claim and funds each with its enrollment gas.
eval "$(scripts/claims.py ptb /secure/claims-2026-10 --package PKG --policy POLICY --fund-mist 40000000)"
```

**On the device**, at first boot, after `suifleet --provision`:

```toml
# /etc/suifleet/enroll.toml
manifest_path = "deployment.json"
policy_id = "0x…"
claim_key_path = "/boot/suifleet-claim.key"
name = "pi-0042"
keypair_path = "/etc/suifleet/device.key"   # or a [signer] table, see below
```

```bash
suifleet --enroll /etc/suifleet/enroll.toml
# {"digest": …, "device_address": …, "device_object_id": …, "device_cap_id": …, "owner_cap_id": …}
```

**The enrollment transaction** has two signers:
- **Sender: the device key.** The signature proves the device holds the key that becomes its trusted identity.
- **Gas sponsor: the claim key.** It proves possession of the claim and pays the gas.

**What the contract does:**
- Checks that the sponsor is an unused claim of an open, unexpired policy with quota left.
- Consumes the claim, registers the Device and takes it into custody.
- Sends the `DeviceOwnerCap` to the operator and a signer-bound `DeviceCap` to the device.
- Emits `DeviceEnrolled`.

**Front-running is impossible:** without the claim key nobody can sponsor with that claim. A claim key is single-use and should be erased after enrollment.

**Operator controls:**
- `revoke_claim` for a lost or leaked claim key;
- `set_limits` for the quota and expiry;
- `close` to stop the policy permanently.

**Groups.** `device_group::add_device` needs the group admin as sender, so the operator adds enrolled devices with one bulk PTB of `device_registry::borrow_device`, then `device_group::add_device`, then `device_registry::return_device` for each device.

**Gas.** Devices need SUI only for status reports. Fund them after enrollment, for example in the same bulk PTB.

**Risk.** A leaked unused claim key lets someone enroll *their* key into your fleet. That device gets nothing it can misuse:
- it only receives your deployments;
- it reports only for itself;
- you hold its owner cap and can revoke it.

Revoke leaked claims, and keep claim batches small, with expiries.

## Automatic Linux first boot

After package or image build, provision the root-owned
`/etc/suifleet/first-boot.toml` on the installed OS during trusted batch staging. Keep this file separate from the writable provisioning
media. The SHA-256 values pin the exact archive members, including line
endings. Obtain them with `sha256sum deployment.json config.template.toml` from a
trusted copy of the extracted archive. If the operator omitted the template,
omit `template_sha256`. Record a finalized checkpoint just **before creating
the batch policy** to bound the recovery search. Use an indexed fullnode that
retains events from that checkpoint.

```toml
policy_id = "0x<POLICY_ID>"
operator = "0x<OPERATOR_ADDRESS_OR_MANAGED_FLEET_ID>"
history_start_checkpoint = 123456
manifest_sha256 = "<LOWERCASE_SHA256_OF_DEPLOYMENT_JSON>"
# template_sha256 = "<LOWERCASE_SHA256_OF_CONFIG_TEMPLATE_TOML>"
# locations = ["/boot/enrollment", "/boot/firmware/enrollment", "/media/provision/enrollment"]

# For a managed batch only, match the archive's [managed] section.
# [managed]
# customer_id = "0x<CUSTOMER_ID>"
# fleet_id = "0x<FLEET_ID>"
# budget_id = "0x<BUDGET_ID>"
```

Mount writable media before the service starts. Each configured location is
the parent of the serial folder. Use a systemd drop-in for custom mount
dependencies or write access outside `/boot`, `/media`, `/run/media` and `/mnt`.
The core Yocto kas profiles enable the service through `meta-suifleet-enrollment`. Custom systemd images using the public layer need the [same enrollment companion layer](CUSTOMER_YOCTO.md). Debian and RPM installations follow their existing
opt-in policy. Enable `suifleet-first-boot-enrollment.service` after preparing
the batch settings, and enable `suifleet.service` for later enrolled boots.

The optional template uses ordinary daemon TOML fields, such as `[apply]`,
`[runtime]`, `[telemetry]`, `[logs]` and `[fleet_data]`. The service supplies
`[sui]` and `[device]`, and derives matching signer settings from the template.
Keep secret values out of the template and provision any credential files
independently. With no template, the client uses a refusing updater.

The service waits for key provisioning and the systemd network-online target.
It runs only while `config.toml` is absent. Before signing, it verifies the
pinned files, deployment, policy registry and operator, then looks up the local
device address in checkpointed enrollment events. It retries chain errors with delays up to 32
seconds for a bounded number of attempts. If the service fails, systemd waits
60 seconds before trying again, up to four starts in 30 minutes. After
confirming the applicable cap and identity, it installs the manifest, isolated
signer settings and daemon settings. The device key stays on the device. The claim file is overwritten,
synchronized and unlinked before the daemon configuration becomes visible.
On flash and copy-on-write media these steps remove the live file. Residual
blocks, snapshots and archive copies require separate media sanitization.

Read `enrollment-status.json` in the serial folder or use
`journalctl -u suifleet-first-boot-enrollment.service`. A chain error can
indicate an offline endpoint, unavailable history or a claim the policy no
longer accepts. If automatic retries are exhausted, correct the cause and use
`systemctl reset-failed suifleet-first-boot-enrollment.service` followed by
`systemctl start suifleet-first-boot-enrollment.service` with the same media.
Keep the existing device key and the original claim with the unit until the
service reports completion.

## Host provisioning media writer

Use [Write one claim per medium](CUSTOMER_ENROLLMENT.md#write-one-claim-per-medium)
to install credentials from the dApp tar archive onto a mounted FAT or exFAT
volume. The writer reserves the claim in a host-side SQLite database before
writing it, then stages and verifies the credential directory before renaming
it to `enrollment/` on the same volume. The database records each serial,
generated volume marker, assignment state and a SHA-256 fingerprint of the
random claim seed for duplicate detection. Keep the database on a local
filesystem outside the provisioning volume and preserve it across retries and
across archives that might include the same claim. The tool refuses existing
enrollment directories without a matching assignment.

## Address-free bulk enrollment in the dApp (SF-100)

The dApp's **Enrollment** page puts the claim flow above behind names and serial numbers. Operators never copy a blockchain address and approve one transaction per batch chunk, not per device.

**Workflow:**
1. **Enrollment batch** (an on-chain enrollment policy). The page selects a batch that accepts enrollments (open, unexpired, quota left); *Create another batch* sets a quota and an expiry. *Set limits* and *Close batch* are under *Change limits or close this batch*.
2. **Add devices**, three ways:
   - *Import CSV*: a `serial` column (required), `alias` (or `name`) and `group` (group name or object ID). A naming template fills blank aliases: `{n:4}` is the row number zero-padded to 4 digits, `{serial}` the serial.
   - *Pair one device* (the default): a serial, an optional name (alias; blank uses the serial) and a group for a single device.
   - *Import roster file*: the public `roster.json` from `scripts/claims.py generate --roster` (below), or an exported job.
3. **Checking** happens as you type. It rejects, per line, malformed serials or aliases, duplicates inside the roster, and conflicts with the operator's devices or other reservations. Both serials and aliases are compared case-insensitively.
   - Allowed characters: 1–64 letters, digits, `.`, `_`, `-`, starting alphanumeric.
   - Unknown or ambiguous group names are rejected too.
   - Imported roster and job files get the same checks; only accepted rows, with their own alias, claim and group, are added.
4. **Download credentials.** A warning before the button explains that the archive is the only copy of its claim keys. The download stays disabled until you tick *I understand this download is the only copy of these credentials*, once per download. The browser generates one fresh claim key per accepted reservation and downloads a tar archive. Each device gets one folder, `enrollment/SERIAL/`, containing:
   - `claim.key`, mode 0600;
   - `enroll.toml`, with the policy, the alias as `name` and `keypair_path = "/etc/suifleet/device.key"`;
   - `deployment.json`;
   - optionally, the batch's `config.template.toml`.
5. **Run pending transactions.** These register the claims and fund each claim's enrollment gas, at most 250 claims per transaction.
6. **Devices enroll themselves.** On packaged Linux, the [first-boot service](#automatic-linux-first-boot) provisions the local identity and consumes the claim. The manual command remains available for individual installations.
7. **Run pending transactions** again to bind each enrolled device. This writes metadata `suifleet.alias` and `suifleet.serial` and adds the device to its group, at most 100 devices per transaction.
   - Binding does not need the policy to accept enrollments. Under a closed, expired or exhausted policy, the run skips claim registration and still binds devices that already enrolled.
   - Devices, groups, access and deployment pages then show the alias and serial.
   - Addresses and object IDs appear only under *Technical details*.

**Binding rules.** A reservation is bound only to the device in the on-chain `DeviceEnrolled` event for *its* claim, and only when all of these hold:
- the event is under the job's policy;
- the operator is the connected wallet;
- the wallet holds that enrollment's `DeviceOwnerCap`.

The device's transaction signature proves possession of its key. The alias is never a credential: a name alone cannot register, authorize or bind a device. The name a device chooses at enrollment is not authoritative; the operator's alias replaces it on display.

**Statuses are derived from the chain on every refresh**, so an interrupted, rejected or repeated run reconciles idempotently:
- unregistered claims are added;
- underfunded claims are only topped up;
- devices that already have their metadata and group are skipped.

| Status | Meaning |
|---|---|
| Reserved | Claim not yet registered |
| Needs enrollment gas | Claim registered, but holds less than the job's enrollment gas |
| Funded, unused | Registered and funded; the device has not enrolled |
| Consumed, pending binding | Enrolled; alias, serial or group still to apply |
| Bound | Alias, serial and any selected group are applied |
| Revoked | Revoked before use |
| Claim withdrawn | Registered earlier and now missing from the policy (revoked elsewhere, or an uncertain submission). It is never re-registered without the explicit *Re-register withdrawn claims* action. |
| Conflict | The alias or serial is now used by another device, or the group is gone |
| Not yours | Enrolled, but the wallet no longer holds the owner cap (transferred or deregistered) |

Each failure is shown against the entries of its transaction.

**Credential protection:**
- Claim keys exist only in memory until they are written into the downloaded archive. The archive is their only copy.
- Jobs are saved in the browser's `localStorage` and can be exported. They contain public data only: names, serials, groups, claim addresses and digests.
- Imported files that contain key material are refused, as are files for another operator, policy, registry or network.
- No key is placed in a URL, log or the tracker.
- Device identity keys are never generated by the browser or put in shared images.

Keep the archive offline, copy each folder only to that device's private provisioning media, and delete it after enrollment. Claims stay single-use, expire with their policy and can be revoked (*Revoke unused claims*). The risk of a leaked unused claim is described above.

**Lost credential file.** A lost archive cannot be downloaded again. Open *Lost a credential file?* under the job and enter the serials it contained:
- *Funded, unused* or *Needs enrollment gas*: *Revoke lost claims* revokes only those claims, after a confirmation. The entries are marked revoked first, so an interrupted revocation is never re-registered automatically.
- *Reserved*, *Revoked* or *Claim withdrawn*: no live claim remains. *Remove reservations to reissue* frees the serial. Reserve it again in step 2 to download a new file.
- *Consumed, pending binding* or *Bound*: the device already keeps its own key. Continue binding if needed.
- *Confirming registration*: refresh until the outcome is known before revoking.

**Transaction feedback.** A failed step shows inline guidance with optional technical details instead of a browser alert:
- A wallet rejection, or a check that failed before submission, is safe to retry.
- A transaction that executed and failed did not take effect; its network fee may still be charged.
- An unconfirmed submission says not to try again until you have checked your wallet activity and refreshed.

**Gas.**
- *Enrollment sponsorship* is the claim's funding. It pays only the enrollment transaction (0.04 SUI default, at most 0.1 SUI from the page). On testnet in November 2026 an enrollment needed a 0.028 SUI gas budget, so claims funded below that cannot enroll.
- *Ongoing device gas* for status reports is a separate *Fund enrolled devices* action with its own amount.
- An unused claim's remaining funding stays on the claim address. Fund claims close to the real enrollment cost.

**Large batches offline.** The browser generates at most 5,000 credentials per download. For more, or for an air-gapped machine:

```bash
scripts/claims.py generate --roster devices.csv /secure/batch-07 --template 'pi-{n:5}' \
  --policy POLICY --manifest deployment.json
# /secure/batch-07/enrollment/SERIAL/{claim.key,enroll.toml,deployment.json}: private media
# /secure/batch-07/roster.json: public; import it on the Enrollment page
```

It refuses malformed or duplicate rows before writing anything. `scripts/claims.py ptb` prints one funded `add_claims` PTB per 250 claims, if you prefer the CLI to the page for registration. Each command's gas budget scales with its claims (1.01 SUI for a full chunk). The gas coin must hold that budget plus the claims' funding (10 SUI for 250 claims at the default 0.04 SUI).

**Measured capacity, not claimed capacity.**
- `make test-bulk-enrollment-localnet` registers 260 claims in two transactions on a disposable localnet. 101 devices enroll, and are bound in two transactions.
- Measured there (2026-10-21, localnet reference gas price; storage, mostly rebated when objects are deleted, dominates):

  | Transaction | Size | Net gas |
  |---|---|---|
  | Register and fund 250 claims | 22.6 KB | 0.658 SUI |
  | Bind 100 devices (alias, serial, group) | 49.1 KB | 0.155 SUI |
  | `scripts/claims.py ptb`, 250 funded claims via `sui client ptb` | 20.0 KB | 0.658 SUI (0.662 SUI gross, budget 1.01 SUI) |

  The 101 sequential self-enrollments took 113 s in total.
- The chunk sizes (250 claims, 100 binds, 250 transfers) stay well inside Sui's PTB limits: 1,024 commands, 512 arguments per command, 16 KiB per pure argument, 128 KiB per transaction.
- Nothing here establishes 100,000-device operation. That is SF-090 ([#177](https://github.com/tyler-suifleet/suifleet/issues/177)). Known scale limits:
  - A `DeviceGroup` stores member IDs in one vector inside a ≤250 KB object, so one group holds at most about 7,800 devices. Each `add_device` scans the vector, so its gas grows with group size.
  - The page scans all `DeviceEnrolled` events and reads one claim-table entry and one balance per pending reservation. Its table shows the first 200 entries per status filter.
  - Every transaction chunk needs one wallet approval: 100,000 devices are about 400 claim and 1,000 bind approvals.

## Scoped deployer roles

**Granting** (operator):

```bash
sui client ptb --move-call PKG::roles::new_deployer_role --assign b \
  --move-call PKG::roles::allow_group b @GROUP_A --move-call PKG::roles::allow_group b @GROUP_B \
  --move-call PKG::roles::grant b @REGISTRY @DELEGATE --gas-budget 50000000
```

**Deploying** (the delegate, holding the `DeployerRole`): `roles::create_deployment_as(role, registry, component, version, recipe_blob, artifact_sha256, recipe_sha256, [groups], clock)`.

**The resulting record:**
- Its `creator` is the **operator**, so devices apply it exactly like the operator's own. No daemon change is needed.
- The delegate is recorded under `DelegateKey` (`deployment_manager::delegate`), and a `DelegatedDeployment` event is emitted.

**What delegates cannot do:**
- target devices directly;
- use groups outside the role, or groups the operator does not administer (checked at grant);
- delete records (only the operator can);
- keep using a revoked role: `roles::revoke(registry, role_id)`, by the operator, ends it permanently.

**Older package versions** cannot create operator-attributed records: the entry point exists only in the upgrade that introduced it.

## Device signers

| Backend | Config | Sui scheme | Status |
|---|---|---|---|
| ed25519 key file | `[device] keypair_path` (default; `suifleet --provision` creates it) | ed25519 | Shipped |
| PKCS#11 token | `[device.signer]`, built with `--features pkcs11` | secp256r1 (P-256) | Tested with **SoftHSM2 2.6.1** only |

```toml
[device.signer]
kind = "pkcs11"
module = "/usr/lib/softhsm/libsofthsm2.so"   # the token's PKCS#11 module
token_label = "suifleet"
key_label = "device"                          # CKA_LABEL of both key objects
pin_file = "/etc/suifleet/pkcs11.pin"        # root-only
```

**How signing works:**
- The private key never leaves the token. Each signature is a raw `CKM_ECDSA` over Sui's secp256r1 prehash: SHA-256 of the Blake2b-256 intent digest. The signature's S value is normalized to the low half.
- The device address is Blake2b-256 of `0x02` and the compressed public key, which the code reads from `CKA_EC_POINT`.

**Algorithm support.** Sui accepts ed25519, secp256k1 and secp256r1 signatures.

| Hardware | ed25519 | secp256r1 (P-256) | Notes |
|---|---|---|---|
| TPM 2.0 | rarely | yes (ECDSA P-256) | Use the TPM through a PKCS#11 provider (`tpm2-pkcs11`) with `kind = "pkcs11"` |
| PKCS#11 HSMs | some (CKM_EDDSA) | yes | P-256 is supported here; ed25519 tokens are not wired up yet |
| Raspberry Pi 3 B+ | none | none | No TPM or secure element on the board |

**Rotation and recovery.** These use SF-008's lifecycle with the owner cap:
- **New key:** generate a key in the token, then `device_registry::rotate_device_key` to its address, then issue a cap for the new address.
- **Lost or compromised token:** `revoke_device`, then rotate once a replacement exists.
- The journal binding includes the signer address, so a rotated device needs a fresh `state_dir` or a journal migration.

**Not claimed:**
- No hardware TPM or HSM has been tested. The PKCS#11 path ran against SoftHSM2, a software token implementing the same interface.
- Attestation (proving a key is hardware-bound) is not implemented.
- Validate a specific TPM or HSM with this procedure before claiming hardware identity: generate a P-256 key, enroll, report, rotate, revoke.

## Tests

- `sui move test`, 18 cases:
  - enrollment: sponsored path, bulk, replay, unsponsored, unknown sponsor, sponsor equal to device, expiry, quota, closed policy, revoked claim, operator-only;
  - roles: operator attribution, operator delete, delegate cannot delete, revoked role, out of scope, foreign group, operator-only revoke, empty targets.
- `make edge-test` runs the pkcs11 feature's tests. A P-256 signature produced through the new encoding verifies with Mysten's independent `UserSignatureVerifier`. Address derivation and high-S normalization are covered too.
- `make test-manifest` runs `scripts/test_enrollment_frontend.cjs` and `scripts/tests/test_claims.py` (SF-100, offline): rosters, templates, conflicts, credential archives, key-free jobs, chain-derived statuses and chunked plans.
- `make test-bulk-enrollment-localnet` (SF-100; disposable network-none localnet, headless Chromium, real daemon): bulk chunked registration/binding with resumption, refusals of replayed, revoked, expired, unsponsored and cross-fleet claims, and the real pages end to end. The page run also covers duplicate serials in an imported roster and binding under a closed policy.
- `make test-enrollment-localnet` (real localnet, SoftHSM2 2.6.1, OpenSC): all 6 scenarios PASS. They cover bulk enrollment of zero-SUI devices, refused claim misuse, a delegate's deployment applied and receipted by the real daemon, refusal outside scope and after revocation, and a SoftHSM-backed device enrolling and reporting Applied.
