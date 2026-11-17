# Deliver confidential components with Seal

Encrypt proprietary recipes and artifacts before upload, then allow devices to decrypt under an explicit policy.

## Prerequisites

Use a compatible manifest, configured verified key servers, a policy you administer and activated device identities. Validate provider authentication, fees, availability and recovery for your own environment; public testnet providers offer no production durability guarantee.

## Steps

1. [Create an access policy](CUSTOMER_ACCESS_POLICIES.md) and grant the intended device/group recipients.
2. Choose confidential delivery in the publishing flow and select that policy. Review public metadata before uploading; encryption does not make every on-chain identifier private.
3. Configure the device and Linux signer with matching Seal server/threshold settings from the operator's reviewed configuration. Keep any provider API credentials in separate protected runtime files, never frontend bundles or reusable images.
4. Publish and deploy a harmless encrypted component first. The device signs a bounded session, obtains keys from verified servers, decrypts locally and verifies authenticated content/digests before execution.
5. Inspect authenticated device receipts and failure messages before expanding the rollout.

## Expected result

Only policy-authorized active identities obtain keys. Integrity, provider or authorization failure stops application; the client does not retry with plaintext delivery.

## Recovery and limits

Check server identity proofs, network reachability, clock/session expiry, threshold and current policy before retrying. Cached keys and plaintext may remain usable after revocation; offline access is bounded by already available data, not a new online grant. Rotate/re-encrypt when necessary. Seal is not a blanket KMS/HSM replacement or assurance for every regulated workload. Production-provider, physical target and manual-wallet acceptance remain separately unrun.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](CONFIDENTIAL_DELIVERY.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
