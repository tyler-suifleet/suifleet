# Wallet delegation, edge identity and confidential delivery

SF-022 is a customer-readiness requirement for proprietary deployments. Seal-encrypted delivery is implemented and tested locally and on public testnet; see [CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md). Public-artifact smoke tests remain useful but do not establish confidential delivery.

## How an administrator grants another wallet access

An account is an administrator because the relevant objects/policies authorize its address, not because it has a special wallet type. The administrator signs a transaction granting a scoped role or updating an access policy for a recipient address. The recipient continues using its own private key. Granting access never requires sharing the administrator's seed, recovery phrase or private key.

Current [roles.move](../contracts/sources/roles.move) implements a limited deployer role: the operator builds it with `new_deployer_role`, adds groups it administers with `allow_group`, then `grant` transfers the role object to the delegate. `create_deployment_as` checks the active registry entry and group scope. The role does not grant arbitrary administration, direct-device targeting or deletion. `revoke` deactivates its registry entry without retrieving the delegate's object.

The current role object is transferable (`key, store` and public transfer); it is not permanently bound to the initial recipient. Scope and the operator's revocation remain attached to that role ID. Do not describe this as a nondelegable account-bound grant or assume every future role is transferable. Broader feature/RBAC controls are not implemented merely because this role exists.

Seal decryption is a separate access check ([seal_policy.move](../contracts/sources/seal_policy.move)). An `AccessPolicy` names accounts, devices and device groups explicitly, and the holder of its transferable `AccessAdminCap` grants and revokes them with signed transactions. Decryption authorizes no publishing, deploying, administration or device operation. Each operation is enforced at its real contract/API/device boundary; frontend visibility is not authorization.

## No administrator passphrase goes to the edge

The normal software-key path creates a unique private signing key locally on first boot and stores it at `/etc/suifleet/device.key` with mode 0600, owned by the separate `suifleet-signer` service user so the daemon user and its component commands cannot read it ([key isolation](KEY_ISOLATION.md)). It is not a wallet recovery phrase and has no human passphrase in the current key-file format. A matching public Sui address is exposed for enrollment; private signing material is not sent to the operator or baked into the image. Protect storage/access accordingly. See [identity.rs](../suifleet-edge/src/identity.rs) and [first-boot provisioning](../suifleet-edge/meta-suifleet/recipes-edge/suifleet/files/suifleet-provision).

The operator enrolls/authorizes the device's public identity. The daemon obtains report signatures from the device signing service, which signs only this device's status reports for the daemon's main process ([key isolation](KEY_ISOLATION.md)); the contracts check that signer against the active device identity. Current code also has an optional PKCS#11 signer backend; hardware provisioning/PIN handling and physical acceptance are separate from a software key-file test. Do not claim a particular device uses hardware protection without evidence.

For the implemented claim-enrollment path, a separate single-use claim key is provisioned through an authorized channel and funds/signs the sponsored enrollment alongside the device's own signature. The onchain claim is consumed, while the device's independently generated identity remains its signer. This bootstrap credential is not an administrator wallet key or an artifact decryption passphrase. Claim enrollment is not automatically enabled merely by booting a generic image. See [enrollment.move](../contracts/sources/enrollment.move).

## Unattended Seal flow

1. The uploader encrypts proprietary artifact bytes and recipes in the browser before anything reaches a publisher. Public metadata is limited to what [CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md#upload-and-binding) lists.
2. The enrolled edge client certifies a bounded Seal session with its own signer, as a Sui personal message. With the service signer, the [signing service](KEY_ISOLATION.md) holds the session key.
3. The client requests the release's keys from verified key servers. They evaluate the Move approval policy against the device's active identity. Provider API credentials, when a provider requires them, are separate runtime files, never device identity, and never part of public images or frontend bundles.
4. The client decrypts locally, verifies authenticated encryption and the on-chain digests, then executes the configured lifecycle command. The administrator never approves individual downloads, and there is no shared fleet password.

Revocation prevents future key release, subject to key servers observing the chain. It cannot erase keys or plaintext already fetched. Lifetimes, caching, rotation and offline behavior are specified in [CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md#lifetimes-caching-offline-use-and-revocation). See also [Seal security guidance](https://docs.sui.io/sui-stack/seal/security-best-practices).

## Acceptance and commercial scope

[SF-022/#25](https://github.com/tyler-suifleet/suifleet/issues/25) is P0 for customer readiness. It covers:
- encryption before upload;
- scoped grants and revocation;
- headless authentication;
- integrity;
- rotation and cache policy;
- provider costs;
- failure-path acceptance.

Its evidence and limits are in [CONFIDENTIAL_DELIVERY.md](CONFIDENTIAL_DELIVERY.md#evidence). No plaintext fallback exists. [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) adds the real-device encrypted profile when available while preserving the earlier public smoke test.

Never equate this requirement with a blanket KMS/HSM replacement or permission to process all sensitive/regulated data. Select an appropriate threat model and follow [Seal's documented scope](https://www.docs.sui.io/sui-stack/seal). No Mainnet publication, real extension-wallet run or hardware enrollment was performed for SF-022.
