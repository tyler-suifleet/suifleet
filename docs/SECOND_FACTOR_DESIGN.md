# Sensitive fleet actions: proposed second factor (SF-196)

Status: proposal, awaiting explicit user approval. This document does not enable
2FA or establish implementation acceptance. Tracking: [SF-196/#384](https://github.com/tyler-suifleet/suifleet/issues/384).

## Decision requested

Recommend native Sui **2-of-3, equal-weight multisig** custody for organization
administration: the normal wallet, an independently held second signing key, and
an offline recovery key. Every administrative transaction requires two keys.
Use separate devices/custody and separate recovery accounts; two credentials
recoverable through the same compromised Google account are not independent.
The user must approve this custody and recovery tradeoff before implementation.

This deliberately protects all organization administration, including development
groups, rather than relying on a mutable production label. Routine device reports
and bounded device gas draws keep their existing device authority. Sign-in and
read-only browsing remain possible with an individual wallet. No backend, OTP
secret service or hosted approval coordinator is introduced.

## Protected actions and authority inventory

| Action | Required enforcement boundary |
|---|---|
| Publish firmware/component versions | Catalog publisher authority; uploading arbitrary public Walrus bytes alone is not an authorized fleet release |
| Deploy and advance production rollouts | Customer administration, deployment creator, group administration, desired-state revisions and delegated deployer roles |
| Change access policy | Seal policy administration, account/device/group grants and policy rotation |
| Withdraw budget or storage funds | Budget owner capability and managed-customer routes; storage balances and renewal custody supported by the deployed storage API |
| Change credentials or administrators | Every customer credential mutation, adding an alternate admin, removing a signer, changing or disabling protection |
| Handover a fleet | Source and recipient authority, retained deployments, groups, device owner capabilities and external storage authority |
| Change recovery/custody | Threshold-authorized rotation to a newly derived multisig; no single-key emergency override |

Infrastructure UpgradeCap and treasury custody are separate governance boundaries.
Tenant enrollment must never transfer shared infrastructure authority. Align with
SF-185 treasury governance once available; do not assume its unmerged API.
An independently malicious package upgrader remains a threat unless upgrade
authority also uses independently controlled threshold custody.

## Alternatives

| Design | Security | Operator experience / Slush and zkLogin | Cost | Recovery |
|---|---|---|---|---|
| Native multisig (recommended) | Validators require threshold signatures for the same transaction bytes; custody must exclude all single-key alternate paths | Two approvals per admin transaction; Slush/Enoki transaction-signing capability must be checked, with explicit unsupported-wallet guidance. Protocol support does not prove a wallet can sign for a multisig sender | Normally one submitted transaction; larger signature and client coordination overhead | Two surviving independent keys authorize migration; losing two keys is unrecoverable |
| WebAuthn/passkey co-signer | A passkey alone replaces a key; it becomes a second factor only when composed with a threshold or separately enforced approval | Convenient authenticator prompt; browser/RP origin, hardware support, wallet partial-signature and pinned SDK compatibility need validation; existing zkLogin remains a separate factor | Native multisig composition can use one transaction; custom verification adds contract work | Synced passkeys may share the compromised account; independent recovery still required |
| On-chain pending action plus second credential approval | Contract binds exact action parameters, organization, nonce, expiry and policy generation; every old/direct path must be sealed | Existing wallets can submit separate transactions as their own addresses, including zkLogin; clear pending-action inbox but more steps and stale-action handling | Proposal/approval/execution transactions and object storage, plus cleanup | Pre-enrolled recovery quorum can rotate factors; a unilateral reset defeats protection |
| Timelock with cancellation | Gives observers time to react; delay alone does not require a second factor | Existing Slush/zkLogin signing works, but operators must monitor and wait; unattended cancellation requires additional customer tooling | Scheduling/execution/cancellation transactions and retained state | Lost observer/cancel access leaves attacker execution possible; not sufficient alone |

Sui documents native multisig, including zkLogin participation, and the SDK
documents passkey composition. These are protocol/SDK capabilities, not verified
Slush, Enoki or current application integration:
[Sui multisig](https://www.docs.sui.io/develop/transactions/transaction-auth/multisig),
[SDK multisig](https://sdk.mystenlabs.com/sui/cryptography/signers/multisig),
[zkLogin](https://www.docs.sui.io/sui-stack/zklogin-integration),
[passkeys](https://www.docs.sui.io/develop/cryptography/passkeys).

## Proposed implementation and migration constraints

The console would guide enrolling three public signer identities, verifying
independent control, deriving the address, testing a harmless threshold signature,
and reviewing the full authority inventory before activation. Never request private
keys, recovery phrases, OAuth tokens or passkey exports. Each signer must review
the network, organization, targets, amounts and exact transaction bytes; changing
bytes invalidates collected signatures. Expired sessions and ambiguous submission
must fail safely using existing effects verification.

An on-chain organization policy must enforce the configured threshold authority,
including credential and recovery changes. An arbitrary address labelled
"multisig" in browser state is insufficient. Implementation must establish how
the configured multisig public-key structure and threshold are authenticated and
how single-key administrator credentials are retired atomically.

Current `customer::assert_admin` accepts any stored administrator address and
checks `version == 1`. Existing package code remains callable. Merely adding a
new policy dynamic field or a new UI check cannot seal that legacy path.
Implementation must design and test version-isolated authoritative organization
state or another demonstrated legacy exclusion mechanism, preserving public
layouts/type origins. Existing-object migration, capabilities held outside managed
custody, old roles, catalog publishers and retained deployments all need explicit
treatment. Do not show "protected" until every selected authority is migrated or
the console reports the remaining scope as unprotected. Do not imply that the
current handover flow already migrates every modern customer/storage authority.

Recovery would use the surviving two keys to migrate to a new 2-of-3 address and
revoke old authority, with the same inventory and migration checks as activation.
No email reset, support override, unilateral factor disable or automatic downgrade.
If fewer than two independent keys survive, recovery is unavailable. The console
must explain and rehearse this before activation, including partial migration and
interrupted-signing recovery. A compromised console can misdescribe a transaction;
independent signer review remains essential.

## Required evidence after approval

- Enumerate every protected public function and legacy equivalent; negative Move
  tests for direct calls, old packages, alternate admins/caps/roles, policy disable,
  threshold weakening, replay and recovery bypass.
- Exercise existing-object upgrade/migration on an isolated chain. Verify that
  devices continue reporting and that old administrative paths reject writes.
- Frontend unit/browser fixtures, typecheck and production build: setup, partial
  signatures, cancellation, changed bytes, network/account/session races,
  unsupported wallets, effects failures and lost-factor recovery.
- Authorized Testnet evidence for setup, approved action, rejected action and
  recovery, with digests/effects where submission occurs and explicit validator
  rejection evidence when no transaction digest exists. Preserve spend limits.
- Normal security/CI and independent exact-head review. Manual real-wallet,
  production-provider, physical and Mainnet acceptance remain unrun in
  [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
  [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

No functional code, dependency upgrade, chain publication or acceptance test has
been performed for this proposal. The next step is the user's explicit decision
on the recommended 2-of-3 custody and recovery model; implementation follows that
decision and must resolve the migration and wallet integration constraints above.
