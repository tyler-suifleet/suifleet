# Understand fleet authority and encryption

Grant the minimum authority needed for an operator, device or recipient. Deployment authority and confidential-content decryption are separate permissions.

## Prerequisites

Inventory device owner capabilities, group administration, deployer roles, access policies, budget ownership and external credentials. Verify the recipient account address through a trusted channel.

## Steps

1. Choose the permission being granted. A scoped deployer role authorizes only its allowed groups; it does not grant arbitrary administration, direct-device deployment or deletion.
2. For decryption use [Access policies](CUSTOMER_ACCESS_POLICIES.md). A policy grant authorizes key requests, not publishing, deploying or device operations.
3. For device identity use [registration/enrollment](INSTALL_DEVICE_CLIENT.md), never an administrator recovery phrase on the edge.
4. For a change of administration follow [Fleet handover](CUSTOMER_HANDOVER.md) and review all authority domains rather than transferring a single object and assuming completion.
5. Review signed transaction effects and verify the recipient's actual permissions after each change.

## Expected result

The intended account can perform only the granted operation. Devices sign with their own identity; administrators do not approve individual confidential downloads or share a fleet password.

## Recovery and limits

Revoke the appropriate role or policy on compromise and rotate affected credentials. Deployer roles and access-admin capabilities are transferable, so do not assume account-bound nondelegable grants. Revocation stops future authorization subject to observation; it cannot erase plaintext or keys already fetched. Frontend visibility is not an authorization boundary. Production-provider and manual-wallet acceptance remain separate.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](ACCESS_AND_ENCRYPTION.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
