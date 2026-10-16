# Wallet delegation, edge identity and confidential delivery

SF-022 is a customer-readiness requirement for proprietary deployments. Seal integration is planned, not implemented by this documentation change. Public-artifact smoke tests remain useful but do not establish confidential delivery.

## How an administrator grants another wallet access

An account is an administrator because the relevant objects/policies authorize its address, not because it has a special wallet type. The administrator signs a transaction granting a scoped role or updating an access policy for a recipient address. The recipient continues using its own private key. Granting access never requires sharing the administrator's seed, recovery phrase or private key.

Current [roles.move](../contracts/sources/roles.move) implements a limited deployer role: the operator builds it with `new_deployer_role`, adds groups it administers with `allow_group`, then `grant` transfers the role object to the delegate. `create_deployment_as` checks the active registry entry and group scope. The role does not grant arbitrary administration, direct-device targeting or deletion. `revoke` deactivates its registry entry without retrieving the delegate's object.

The current role object is transferable (`key, store` and public transfer); it is not permanently bound to the initial recipient. Scope and the operator's revocation remain attached to that role ID. Do not describe this as a nondelegable account-bound grant or assume every future role is transferable. Broader feature/RBAC controls are not implemented merely because this role exists.

Seal decryption policy is a separate access check to implement in SF-022. Artifact readers/devices must have explicit release/device/group scope; authority to decrypt must not implicitly authorize publishing, deploying, administering or invoking hosted/device operations. Enforce each operation at its real contract/API/device boundary. Frontend visibility is not authorization.

## No administrator passphrase goes to the edge

The normal software-key path creates a unique private signing key locally on first boot and stores it at `/etc/suifleet/device.key` with mode 0600. It is not a wallet recovery phrase and has no human passphrase in the current key-file format. A matching public Sui address is exposed for enrollment; private signing material is not sent to the operator or baked into the image. Protect storage/access accordingly. See [identity.rs](../suifleet-edge/src/identity.rs) and [first-boot provisioning](../suifleet-edge/meta-suifleet/recipes-edge/suifleet/files/suifleet-provision).

The operator enrolls/authorizes the device's public identity. The daemon signs reports with its own key; the contracts check that signer against the active device identity. Current code also has an optional PKCS#11 signer backend; hardware provisioning/PIN handling and physical acceptance are separate from a software key-file test. Do not claim a particular device uses hardware protection without evidence.

For the implemented claim-enrollment path, a separate single-use claim key is provisioned through an authorized channel and funds/signs the sponsored enrollment alongside the device's own signature. The onchain claim is consumed, while the device's independently generated identity remains its signer. This bootstrap credential is not an administrator wallet key or an artifact decryption passphrase. Claim enrollment is not automatically enabled merely by booting a generic image. See [enrollment.move](../contracts/sources/enrollment.move).

## Planned unattended Seal flow

1. The uploader encrypts proprietary artifact bytes and confidential recipe/configuration content locally before sending it to a publisher/relay. Public metadata needed to locate the ciphertext, policy and key servers must be defined without leaking protected content.
2. The enrolled edge client uses its own signer to authorize a bounded Seal session. Software/hardware signing adapters must implement the correct personal-message/session intent; the existing transaction-only signer interface is not proof that Seal authentication already works.
3. The client requests the release's decryption material from verified key services. They evaluate the configured Move approval policy and the authenticated identity. Production provider API credentials, when required, are separate service credentials; they are not a substitute for device identity and must not be included in public images/frontend bundles.
4. The client decrypts locally, verifies authenticated encryption and the specified artifact/recipe bindings, then executes the configured lifecycle command. The administrator does not approve each download interactively, and no shared fleet password is used.

This requires an implemented, tested headless integration. Browser SDK examples alone do not establish Rust/PKCS#11 client support. The exact deployment/versioned namespace and allowed metadata must be reviewed and documented by SF-022. See [Seal usage](https://www.docs.sui.io/sui-stack/seal/using-seal).

Revocation prevents future authorized key release subject to state propagation; it cannot erase keys/plaintext already fetched. Offline cache permissions, expiry, rotation and fresh encryption identities must be explicit. Neither session expiry nor account revocation remotely destroys a retained decryption key. Seal checks do not atomically observe all chain changes across providers. See [Seal security guidance](https://www.docs.sui.io/sui-stack/seal/security-best-practices).

## Acceptance and commercial scope

[SF-022/#25](https://github.com/tyler-suifleet/suifleet/issues/25) is P0 for customer readiness: encrypted-before-upload, scoped grants/revocation, headless authentication, integrity, rotation/cache policy, provider costs and failure-path acceptance. It still depends on the actual identity/role/configuration interfaces; priority does not waive dependencies or authorize a plaintext fallback. [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) adds the real-device encrypted profile when available while preserving the earlier public smoke test.

Never equate this requirement with a blanket KMS/HSM replacement or permission to process all sensitive/regulated data. Select an appropriate threat model and follow [Seal's documented scope](https://www.docs.sui.io/sui-stack/seal). No artifact encryption, delegated UI feature, Mainnet publication or hardware enrollment was implemented or performed by this document.
