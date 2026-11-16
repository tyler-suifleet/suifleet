# Manage access policies

Choose who may decrypt a new confidential release. These policies do not grant deployment or fleet administration rights.

## Prerequisites

Connect the account holding the policy's AccessAdminCap on the intended network. Configure verified encryption key servers and have activated devices, administered groups or complete recipient account addresses ready.

## Steps

1. Open **Access policies** and create a recognizable policy name.
2. Grant an activated named device, a group you administer or a complete Sui account address. A group grant delegates recipient selection through its current membership.
3. Review and sign the transaction, then refresh and confirm the grant succeeded.
4. In **Deploy component**, select that policy when uploading a new confidential artifact and recipe. Changing a policy selection does not rewrite earlier releases.
5. Remove obsolete grants and review current recipients before later releases.

## Expected result

Authorized recipients can request the release's keys through the configured providers. Unauthorized or revoked identities fail policy checks; there is no plaintext fallback.

## Recovery and limits

Retry failed reads before assuming there are no policies. Verify the connected account, network and configured servers if controls are unavailable. Rejected writes preserve inputs; inspect uncertain submission before retrying. Revocation cannot recall cached keys or delivered plaintext. Rotation retires earlier key epochs for new requests; re-encrypt and redeploy content that devices still need.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](ACCESS_POLICIES.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
