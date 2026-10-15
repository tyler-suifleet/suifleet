# Fleet enrollment, deployer roles and device signers (SF-021)

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
eval "$(scripts/claims.py ptb /secure/claims-2026-10 --package PKG --policy POLICY --fund-mist 20000000)"
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
- `make test-enrollment-localnet` (real localnet, SoftHSM2 2.6.1, OpenSC): all 6 scenarios PASS. They cover bulk enrollment of zero-SUI devices, refused claim misuse, a delegate's deployment applied and receipted by the real daemon, refusal outside scope and after revocation, and a SoftHSM-backed device enrolling and reporting Applied.
