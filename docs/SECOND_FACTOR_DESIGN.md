# Optional organization security framework (SF-196)

Status: revised design for user review; not approved or implemented. This replaces
the earlier multisig-only recommendation. The [scope decision recorded at
2026-10-28T01:06:32Z](https://github.com/tyler-suifleet/suifleet/issues/384#issuecomment-6457892237)
selects **all four optional mechanisms**, independently chosen per organization.
The remaining decision is review of this shared design, not choosing one mechanism.
[SF-196/#384](https://github.com/tyler-suifleet/suifleet/issues/384) remains Blocked
pending that review. This recovery changes documentation only.

## Ownership and defaults

SF-196 owns the protected-action taxonomy, authoritative on-chain organization
policy, enforcement boundary, policy-change rules and Security settings page.
The following tickets implement mechanisms against that framework after design
review; this note defines behavioral contracts, not guessed Move signatures.

| Mechanism | Ticket and required behavior |
|---|---|
| Multisig administrators | [SF-201/#400](https://github.com/tyler-suifleet/suifleet/issues/400): register a Sui k-of-n administrator credential, display authenticated members/threshold, collect partial signatures and submit without a server, including unsigned-transaction export/import. Slush, extension and hardware-wallet membership need their specified acceptance. A 2-of-3 arrangement is an example, not a mandatory organization default. |
| Passkey co-signature | [SF-198/#397](https://github.com/tyler-suifleet/suifleet/issues/397): enroll/remove passkeys; require the administrator's enrolled passkey signature **in addition to** wallet authority on protected actions. Passkey-only sign-in does not satisfy this option. Document browser/platform support and lost-device recovery. |
| Second-person approval | [SF-199/#398](https://github.com/tyler-suifleet/suifleet/issues/398): on-chain proposal shows exact action, proposer and expiry; another authorized credential approves or rejects. Proposer cannot self-approve. Dashboard lists pending requests; expired proposals cannot execute. Different credentials are enforceable; different human control also requires organization custody discipline. |
| Delay with cancellation | [SF-200/#399](https://github.com/tyler-suifleet/suifleet/issues/399): per-action-class delay within documented bounds; on-chain queue, countdown and cancel control. Execution before maturity or after cancellation fails; any current administrator can cancel during the window. Explicit emergency exceptions are below. |

All four options default **off** for new organizations. Existing organizations
remain visibly legacy/unprotected until verified migration; absence of a policy
on a legacy object is not proof of an intentionally disabled policy. On migrated
state, a present all-off policy permits ordinary authorized administration.
Enabling one option never silently enables another. Enabled requirements combine
with AND, not OR. Read-only browsing and wallet sign-in remain available without
completing action factors. Device reports and bounded device gas draws retain
their existing scoped device authority, not organization administrative powers.

## Protected actions and authority inventory

Protection applies to organization administration across all fleet environments;
a production label must not provide an alternate authorization path.

| Action class | Required enforcement coverage |
|---|---|
| Publish | Authorized firmware/component catalog publication and publisher changes. Uploading arbitrary Walrus bytes alone is not an authorized fleet release. |
| Deploy and rollout advance | Creation, targeting, desired-state revisions, rollout advance, resume, skip and changes that cause execution; customer, creator, group and delegated deployer routes. |
| Access-policy changes | Seal policy grants/revocations, account/device/group permissions, policy rotation and alternate access capabilities. |
| Budget and storage withdrawals | Budget withdrawal, refund/recipient changes, transfers and storage balances/custody routes supported by the deployed storage API; guard delegated routes too. |
| Credentials and administrators | Add/remove/replace credentials, admin grants, delegation, recovery enrollment, policy enable/disable and any weakening. |
| Fleet handover | Source authorization, recipient confirmation, fleets/groups/devices, retained deployments, budget and external storage authority; no partial handover labelled fully protected. |

Emergency exception: pausing/stopping further deployment execution, rejecting a
proposal and cancelling a queued action have **no delay**. Any current
administrator may cancel a queued action without collecting its execution
factors; cancellation consumes/invalidate its authorization. Pause/stop retains
normal administrative authorization and other enabled factors, but skips the
delay. These operations cannot change targets, release artifacts, withdraw funds,
resume execution or weaken policy. Resume and subsequent deployment use the full
policy. This trades availability (a compromised admin can cancel) for a usable
cancel window. UI and tests must distinguish pause from destructive rollback or
other lifecycle commands; those receive no implied emergency exemption.

Infrastructure UpgradeCap and shared treasury governance remain separate from
tenant policy. Align with SF-185 without assuming an unmerged API. A malicious
package upgrader or independently held external authority is outside a tenant
policy's protection until its custody is independently secured.

## On-chain policy and composition

Proposed policy state binds the organization ID, schema version, policy generation,
per-action-class enabled requirements and factor configuration references.
Configuration includes authenticated multisig structure/threshold, enrolled passkey
public identities, eligible second-person credentials, delay bounds and expiry
rules. Unsupported versions or enabled mechanisms unavailable in the deployed
package fail closed. The UI must not offer an unavailable mechanism as active.
A framework landing before a mechanism may expose it as unavailable; acceptance
of that later mechanism belongs to its linked ticket.

Each protected operation checks authoritative state on chain, including direct
contract calls. One action authorization binds chain/deployment domain,
organization, action class, full canonical arguments and target IDs, relevant
object revisions, policy generation, initiator, unique nonce and expiry. Approval
cannot be reused for different recipients, amounts, artifact digests or targets.
Execution atomically verifies all enabled requirements and consumes the action;
replays and duplicate execution fail. Existing role/capability checks remain
necessary in addition to factor checks. Credentials and target authority must
still be valid at execution; revocation or generation changes invalidate pending
authorizations rather than grandfathering weaker rules.

For example, multisig plus delay requires threshold authorization of the exact
queued intent and execution only after its on-chain not-before time, while still
uncancelled, unexpired and authorized. All four enabled requires threshold wallet
authority, an additional enrolled passkey proof, approval by a different eligible
credential, and the delay. A passkey merely counted as an optional multisig member
does not satisfy a separately required passkey co-signature. Approval from another
member of the proposer's multisig must not be treated automatically as a distinct
organization approver. Thresholds do not justify silently dropping other factors.

Proposed ordering: validate initiator authority and cryptographic requirements,
record the bound proposal/queue, collect any distinct-person approval, then execute
after the delay measured from immutable queue creation. All evidence must be
present at execution; expiry must leave time for the configured delay. Changes to
the action restart authorization and the cancel window. Sui Clock supplies delay
and expiry checks; browser countdowns are informational. Native signature checking
and Move authorization must together prove the required wallet/passkey identities;
a browser flag or arbitrary address labelled multisig is insufficient. Exact
cryptographic representation, serialized action schema and numerical delay/expiry
bounds must be specified and reviewed before mechanism implementation; this note
does not assert SDK/wallet support or a working verifier.

## Policy changes, enrollment and recovery

The Security page reads verified on-chain policy and migration coverage, explains
each option in plain language, shows combinations, fees/extra transactions,
unsupported wallets, pending approvals and queued countdowns. It previews exactly
which factors authorize a change and the resulting recovery consequences.
Enabling requires current authority and proof that newly enrolled factors work;
activation and legacy exclusion must be atomic or remain explicitly incomplete.

Every policy mutation is authorized under the **currently effective** requirements
for policy/credential changes. Where an edit weakens another action class, it also
requires that class's current requirements; requirements combine rather than
selecting the weakest class. Disabling an option, lowering threshold/delay,
replacing a factor, adding an alternate administrator or expanding emergency
exceptions cannot authorize itself under its proposed weaker policy. Successful
changes increment the generation and invalidate outstanding actions. Tightening
also uses current authorization; no unauthenticated policy edit is allowed.

Recovery is explicit and enrolled before activation, never an email/support reset,
UI-only override or automatic downgrade. Recovery authority must satisfy the
current policy or an explicitly preauthorized on-chain recovery path with no
weaker requirements; no such bypass is supplied by this proposal.

- Multisig: surviving quorum rotates custody; 2-of-3 can tolerate one lost key,
  but losing quorum is unrecoverable without a previously authorized path.
- Passkey: use a previously enrolled independent replacement that satisfies the
  policy or the established recovery quorum. Losing every required passkey with
  no authorized recovery path locks protected actions; the wallet alone cannot reset.
- Second-person: another currently eligible independent credential can approve
  recovery. No eligible approver means protected changes remain locked.
- Delay: recovery/policy weakening waits out the existing delay and remains
  cancellable; losing access to observers does not remove the window. Delay alone
  is an optional reaction window, not proof of a second independent factor.

With combinations, recovery satisfies every enabled requirement. Rehearse recovery
before activation, warn about permanent lockout, and recommend separate devices,
accounts and recovery custody. Synced passkeys and wallet recovery through the
same compromised account do not provide independent protection. Never collect
private keys, recovery phrases, OAuth tokens or passkey exports.

## Old-package bypass and migration boundary

Current `customer::assert_admin` accepts stored administrator credentials with
`version == 1`. Older packages remain callable. Adding a policy dynamic field,
a new wrapper or frontend checks cannot make old bytecode enforce it.

The proposed boundary is version-isolated authoritative organization state:
retire the legacy authority version through an authorized migration and route
protected administration through policy-aware state. Preserve existing public
layouts/abilities and per-type introduction origins. A version bump is only useful
where every legacy writer actually checks it. Inventory every public function and
legacy equivalent, standalone capability, role, catalog publisher, budget/storage
owner and retained deployment. Demonstrate legacy rejection for each or move its
authority into exclusive policy-aware custody. External capabilities that cannot
be sealed remain outside the protected scope and prevent a complete activation
claim. Migration must not strand routine device reporting or authenticated reads.

Do not assume initialization reruns, handover migrates all authorities, or changing
the manifest disables old packages. Inventory existing objects and administrator
alternates before migration; bind migration to source authority, destination policy
and recipient confirmation. Interrupted migration must preserve an honest partial
state with no protected badge until exclusion is verified. Record new IDs/type
origins in the deployment manifest and synchronize clients. Exact per-module
migration transactions and rollback constraints remain implementation design work;
no legacy bypass is claimed fixed here.

## Serverless operation and required evidence

Preserve SF-137: static console, wallet/browser signing, on-chain policy/proposals/
queues and direct verified chain reads. Partial signatures travel through explicit
export/import or user-selected sharing; no SuiFleet signing server, OTP-secret
service, approval coordinator or mandatory monitoring daemon. Execution after a
delay is explicitly submitted by an authorized actor, not automatically scheduled
by a server. No availability promise for unattended cancellation. Signers review
network, organization, targets, amounts and exact bytes independently; effects,
not a returned digest, establish success. Ambiguous submissions require chain
reconciliation before retrying.

After user review, SF-196 must implement and verify its framework before Review;
each mechanism ticket retains its own tests and acceptance:

- Enumerate protected functions and legacy routes; Move tests for all-off defaults,
  organization isolation, direct calls, unsupported policy, all combinations,
  weakening/recovery, replay, revoked credentials and policy-generation changes.
- Negative old-package/alternate capability tests and real existing-object upgrade/
  migration on an isolated chain; verify device reporting remains functional.
- Mechanism suites: missing/changed partial signatures and passkeys; self-approval,
  rejection and expired proposals; early/late/cancelled/duplicate queue execution;
  immediate cancellation/pause and protected resume; lost-factor recovery.
- Frontend tests, typecheck and production build for Security settings, enrollment,
  combinations, pending dashboard, countdown/cancel, network/account/session races,
  unsupported browsers/wallets, effects failure and interrupted migration.
- Authorized testnet setup, allowed/refused action and recovery evidence with
  digests, execution effects and net gas; record pre-submission rejection honestly
  when no digest exists. Preserve wallet locks and spending controls.
- Security checks, exact-head/current-base CI and independent review. Final manual
  wallet/hardware-key, production-provider, physical and actual-mainnet acceptance
  remain unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
  and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

This design repair runs document consistency checks only. No mechanism, framework,
Security page, migration, functional test or chain transaction is implemented by
this note. Next step: user review of the revised shared design; retain Blocked
until that review, then resume this existing claim within the authorized scope.
