# Optional organization security framework (SF-196)

Status: user approved this shared design on 2026-10-31 in
[#384](https://github.com/tyler-suifleet/suifleet/issues/384#issuecomment-6552386259).
The earlier human design-review hold is obsolete. All four protections remain
optional, independently chosen per organization. Implementation and automated
acceptance must complete before Review; approval does not establish enforcement.

## Ownership and defaults

SF-196 owns the protected-action taxonomy, authoritative on-chain organization
policy, enforcement boundary, policy-change rules and Security settings page.
The following tickets implement mechanisms against that framework below; their independent implementation ownership remains unchanged.

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

SF-196 must implement and verify its framework before Review;
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

## Implemented framework and child API (2026-10-31 recovery)

The shared framework is independently useful: it establishes policy authority,
source/custody boundaries, canonical single-use actions and the settings view.
It does **not** implement the four mechanisms. The deployed availability mask is
zero; nonzero activation fails on chain until a child supplies its actual verifier,
authenticated enrollment, UI and acceptance. Unavailable options are visibly
disabled. Test-only attestations are not production evidence.

Authority version **197** excludes both legacy version 1 and the already-published
intermediate framework version 196. `initialize_security` migrates either under
its current administrator authority (and existing requirements for 196); it
preserves existing policy state and invalidates pending actions. Old 196 bytecode
must not remain authoritative after later factors activate. Existing layouts and
public signatures remain compatible. Never infer migration from a missing field.
Coordinate the core/client/producer upgrade before migration; stale producer
bytecode is deliberately excluded, not automatically migrated. The daemon accepts
legacy 1/current 197 custody; the console also discovers 196
so it can identify the required migration, without presenting it as current policy.

Every fleet needs a custody seal before nonzero activation. New empty fleets are
sealed automatically. Existing fleets use `seal_security_fleet` only after
`pending_devices == 0`; adoption/retirement seals a reconciled fleet when its last
pending capability is resolved. Adopting a populated unreconciled budget into an
already protected customer fails closed, including inside a wrapper grant.
Standalone wallet-owned objects remain outside managed organization custody.

### Canonical action contract

- Bits: multisig=1, passkey=2, distinct-person=4, delay=8. The seven entries are
  publish, deploy/advance, access, withdrawals, credentials/policy, handover and
  other administration. Enabled bits combine with AND.
- `prepare_security_action(customer, intent, expires_ms, clock, ctx)` requires a
  current administrator. `Action` binds customer, policy generation, proposer,
  immutable 32-byte intent and expiry (strictly future, at most seven days).
  Its unique shared object ID is the nonce. It has no store/public-transfer
  ability. Execution or current-administrator cancellation consumes it.
- Canonical arguments are BCS `vector<vector<u8>>`: BCS operation-name String,
  followed by BCS of every explicit argument in declaration order, excluding
  Action, Clock and TxContext. References use the complete current object BCS;
  owned values use their complete BCS. This binds both IDs and current contents.
  Group operations append BCS desired-state revision; deployment-reference
  operations append `deployment_manager::security_snapshot` (BCS vector of
  control, target groups/devices and optional rollout state); enrollment edits
  append their security revision. Storage wrappers bind customer/fleet/storage/
  budget contents, exact amounts and recipient. `remove_device_secured` includes
  the ultimate recipient; source enrollment includes the original witness type.
- `security_policy::action_intent(customer_id, arguments_bcs)` computes
  Blake2b-256 of BCS `vector<vector<u8>>` containing BCS original `TypeName<Action>`,
  BCS customer ID and those argument bytes. The introduction-package identity
  and globally unique chain-created customer ID form the deployment/customer
  domain. Clients must also verify the manifest chain and per-type origins;
  a caller-supplied network label has no authority.
- An execution wrapper consumes Action, recomputes this intent, checks current
  generation/proposer/expiry and enabled requirements, executes one operation,
  then clears its transaction-local grant before returning. Complete object BCS
  and dynamic revisions reject stale state even if a direct all-off write did
  not increment the policy generation. Credential/policy/source changes and
  completed secured operations invalidate outstanding actions.
- Ordinary operations enforce their own class. Raw recipe deployments, rollouts
  and imported deployments also require publication protection; foreign catalog
  versions require it at import. Group adoption/membership changes require deploy
  and access protection as well as administration. Custody release requires
  handover protection. Policy changes, administrator changes, source changes and
  whole-organization handover conservatively require the current union of classes.
- `pause_deployment_secured` changes only paused state and control revision. It
  preserves schedule/targets and skips only delay; other factors remain required.
  Resume uses the full deployment requirements. Cancellation requires a current
  administrator but no execution factors. Neither operation grants arbitrary
  lifecycle-command, reboot or recovery authority.

### Trusted mechanism and external-source boundary

Children SF-198/#397, SF-199/#398, SF-200/#399 and SF-201/#400 require this reviewed,
merged prerequisite. Their exact in-package hooks are:

- `assert_security_administrator`, `begin_security_action` and
  `finish_security_action` in customer are package-only. Child wrappers compute
  their own canonical arguments and retain the existing authority checks.
- `security_policy::attest` is package-only and accepts one factor bit. A child
  authenticates the configured source, exact Action ID/intent, generation,
  credentials and expiry before attesting. No public generic factor witness exists.
- `configure_security_factor` / `configure_factor` are package-only. Replacement
  requires every currently effective class, a bounded nonempty public binding,
  and invalidates queued proofs. `security_factor_configuration` is read-only.
  A child must define/verify its binding schema and prove newly enrolled factors
  work; storing arbitrary bytes never establishes a working factor.
- Generic `borrow_budget_cap` cannot inherit a secured grant: any nonzero class
  disables that loan. This prevents a caller approving one loan and substituting
  an unrelated withdrawal, data mutation or custody change before returning it.
- Storage withdrawals have real operation-specific wrappers in fleet_storage:
  `withdraw_managed_wal_secured`, `withdraw_managed_storage_secured` and
  `withdraw_managed_retention_secured`. Only that enrolled producer can construct
  its non-store `CustomerCharge` witness. The base `begin_storage_action` fixes
  class 3, checks the exact witness identity/current administrator/action, and
  binds the cap to the customer fleet. The storage wrapper performs the exact
  withdrawal, restores the cap and clears the grant before returning. The witness,
  cap and loan are never returned by the external wrapper.
- Storage source enrollment keeps registry authority and adds current customer
  requirements; `configure_customer_charging_secured` binds the original source
  type and exact customer/registry. It cannot replace an existing source with a
  different type. A storage package upgrader remains an explicitly trusted code
  authority. Other generic external data/storage administration stays unavailable
  under protection until a reviewed exact-operation adapter exists; no public
  arbitrary-class/witness or raw-cap escape is provided. Device-scoped bounded
  reporting, gas and storage writes retain their existing authority.
- Enrollment grants bind a separate enrollment-authority epoch, stored under
  `enrollment::SecurityGenerationKey` on the Customer UID; the same existing key
  on each EnrollmentPolicy UID holds its authorized epoch. Missing fields read
  as zero, without changing layouts or introduction origins. Administrator removal
  or demotion, changed policy requirements, factor-source configuration, handover
  and migration from the intermediate v196 authority increment this epoch.
  Adding credentials, re-saving an unchanged all-off policy and completing an
  unrelated secured action do not revoke provisioned claims. Queued Actions still
  use the broader policy generation and retain their existing invalidation rules.
  After a genuine authority revocation, a current administrator can reauthorize
  the policy's remaining claims using `add_claims` (an empty vector is sufficient),
  or `add_claims_secured` with the currently required factors. Review/revoke leaked
  claims before rebinding; rebinding authorizes all remaining claims on that policy.
  Claims previously bound to an action generation by the intermediate framework
  may need this explicit reauthorization during migration. Managed policies remain
  closed to old standalone enrollment routes.

Catalog adoption changes publisher custody to the customer object address, so
old wallet-publisher entry points cannot publish. Current publication checks the
publish class and exact arguments. Whole-organization handover consumes a separately
signed, expiring recipient consent bound to customer/generation, revokes all source
credentials and retains fleets, deployments, budgets, adopted publishers and
external storage authority in the same object namespaces. It does not advertise
partial asset extraction as whole-fleet protection.

New types are a reviewed optional introduction-origin group (including policy,
actions, configurations, custody keys and handover consent/events). Each keeps
its actual introduction package; a repair upgrade does not rewrite those origins.
See the generated interfaces and the actual local/testnet receipts in
[the recovery evidence](validation/sf196-framework-recovery.md).

Final human/extension-wallet, provider, physical and mainnet acceptance remains
unrun in #32/#159. The child mechanisms and their activation/enrollment UI are
still their respective tickets; this framework must not be advertised as four
completed second-factor mechanisms.

### SF-198 native sponsorship

Draft only: package-size and required testnet acceptance blockers remain in
SF-198/#397. This section describes the proposed implementation, not acceptance.

SF-198 uses Sui's native verified passkey **gas sponsor** plus the existing
administrator wallet sender. Both sign the same TransactionData bytes. The
passkey factor is authenticated from `TxContext::sponsor`, not a transferable
Action attestation or browser-reported signature type. Its address is
Blake2b-256(`0x06 || compressed P-256 public key`). Native enrollment transactions
prove possession before a currently authorized administrator installs the key.
Bindings use BCS `vector<address>` alternating administrator/passkey addresses;
all existing factor requirements authorize changes. Empty bindings cannot enable
protection. Native sponsorship satisfies only bit 2; the other mechanisms remain
unavailable pending their owners. The transaction signature makes generic
capability loans safe for passkey-only requirements: all cap uses are covered by
the exact signed PTB. Intent-based attestations and other requirement bits still
cannot authorize these generic loans. See the operational enrollment and recovery
procedure above in [OPERATIONS.md](OPERATIONS.md#optional-passkey-co-signature-sf-198).
