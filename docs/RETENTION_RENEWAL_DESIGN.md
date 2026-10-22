# Protected prepaid retention: SF-092 implementation design

Status: original design, retained as acceptance intent. Implementation is in progress;
see [operator guide and explicit remaining gaps](RETENTION_RENEWAL.md). SF-092 is
not Review-ready; this design is not test evidence.

## Pinned prerequisite and integration gate

The published SF-083 interface is version 1 at
`57cd3bc1aec364d302b7192c8c19eb550c10aa23`, in
[PR #218](https://github.com/tyler-suifleet/suifleet/pull/218).
SF-083 merged at `3e796f7c2a91e88734970b0d3956469e3a86c7ed`; actual current main
was integrated in `6db2ee90eb05605e445034097903f0680e30898a`. The original pin
and consumed v1 boundary remain recorded below; the dependency blocker is resolved.
The implementation and merge prerequisite is
[SF-083/#161](https://github.com/tyler-suifleet/suifleet/issues/161).
Implementation against the published pin is allowed; this is not integrated
acceptance. Keep the consumer PR draft until SF-083 is verified Done/merged,
current main is integrated and compatibility and end-to-end checks pass.

The consumed v1 surface is:

| Surface | Use and boundary |
|---|---|
| `FleetBudget`, `BudgetOwnerCap`, `GasAccount` | Fleet authority and per-device funding; never delegate the owner cap |
| `balance`, `spendable`, `reserved` | v1 spendable equals balance; reserved is zero, not protected retention |
| `assert_owner`, `assert_may_draw`, `migrate` | Existing authority, identity, pause and version checks must survive |
| `FleetStorage`, `BlobRecord` | Keep existing layouts and per-type introduction origins |
| `blob_record`, `pool_epochs`, `wal_balance` | Read actual blob expiry, unused pool expiry and WAL independently |
| `register_blob`, `register_fleet_blob` | Discretionary writes currently draw from shared WAL |
| `extend_blob`, `release_blob` | v1 requires the owner cap; neither is unattended executor authority |
| Deposit, draw, upload and extension events | Accounting inputs, accepted only from successful execution effects |

Coordination proposals are recorded in
[SF-092](https://github.com/tyler-suifleet/suifleet/issues/179#issuecomment-6347887152)
and [SF-083](https://github.com/tyler-suifleet/suifleet/issues/161#issuecomment-6347887499).
They are proposals rather than an agreed or implemented new public interface.

## Contract extension and migration

Preserve v1 public signatures and object layouts. Introduce separate dynamic-field
policy/accounting types and record their introduction-package origins. A protected
renewal balance must be distinct from discretionary WAL and device gas accounts.
Moving WAL into this balance removes it from every discretionary spend path,
including owner uploads and pool purchases. Protect measured bounded execution
SUI separately; accounting labels alone cannot reserve coins.

Bump affected mutation versions and provide explicit owner migrations. The
migration must initialize protected accounting before discretionary writes resume.
Test attempts through every previous spend, withdraw, release and renewal path.
Old packages remain callable: a new UI balance or getter is not an authority
boundary. Do not claim compatibility from source-level checks alone. Exercise
real upgrades with existing budgets, device accounts, wrapped blobs and pool
resources. Verify package linkage when storage calls the upgraded core package.

The owner retains withdrawal and override authority. An explicit override must
identify the policy/reserve reduction and affected blob expiries in effects/events
and UI. A subsequent dashboard must show protection as at risk. This is a finite
prepayment policy, not a promise that the owner cannot withdraw or that funds
cover arbitrary future prices.

Policy installation must authenticate a complete closure of active artifacts,
recipes, configuration and dependencies, designated rollback/recovery artifacts,
and explicitly selected site/manifest/history objects. A caller-supplied list of
blob IDs without closure validation is not sufficient. Removing or replacing a
protected reference requires owner authority and an observable policy revision.

## Bounded executor

Use an explicitly configured owner-run process, independent of the browser, with
a separate restricted executor identity. It must never receive the owner cap.
The exact new callable API and signer policy remain to be implemented and reviewed.
The proposed authority permits only protected blob IDs, owner-set target epochs,
safety margins, quote ceilings, per-period renewal allowances and bounded execution
costs. It cannot withdraw, change policy, release blobs, register arbitrary blobs,
or renew an unapproved target.

The owner must run and fund the process. It pays transaction gas from a separately
bounded float; no always-on privileged sponsoring key is introduced. If that float
is empty, the executor is stopped or the network is unavailable, renewals do not
occur. Report that condition rather than claiming the contract schedules work.
Any gas reimbursement design must bound total reward per successful renewal and
period; duplicate or no-op attempts must not drain the gas reserve.

Read the Walrus epoch and exclusive blob end epoch. Renew while the blob is still
live, within a configurable margin and the protocol's actual extension horizon.
Read a fresh WAL quote and reserve its bounded cost before admitting discretionary
writes. Keep SUI and WAL units explicit; a SUI price estimate is not WAL available
for storage. If prices exceed the approved ceiling, preserve reserves and warn.

Persist an intent before submission, including policy revision, blob, observed end,
target and transaction identity. On restart or uncertain submission, reconcile
execution effects and authoritative blob expiry before another attempt. Serialize
shared reserve accounting on-chain; reject stale targets and revisions. Record
success only after successful effects and a verified new expiry. Cap attempts,
backoff and total gas; record failure rather than indefinitely resubmitting.

## State, notifications and bounded queues

Expose independent facts instead of one payment status:

| Fact | Required evidence or behavior |
|---|---|
| Purchase failed or uncertain | Provider state is separate; no prepaid credit without confirmed deposit effects |
| Low spendable funds | Actual discretionary balance below configured threshold |
| Protected retention | Policy closure, protected balance, quote and covered horizon; explicitly finite |
| Operation allowance exhausted | Current per-device/period quota; existing apps keep their configured policy |
| Renewal failed | Last attempt, verified error, retry budget and last successful extension |
| Blob expired | Current epoch at or beyond exclusive end; restoration required |

Show actual epoch expiry and only label a calendar date as projected when derived
from an identified epoch schedule. Show last successful renewal, quote assumptions,
projected runway and persistent failure independently. Receiving funds does not
grant fleet authority. Replenishment from another supported wallet/payment method
must confirm on-chain deposits and preserve existing owner/device authorization.
A declined checkout does not consume prior prepaid funds, delete installed apps,
revoke identities or trigger uninstall, reboot or platform rollback.

An optional, explicitly configured owner-run notifier reads these states and sends
minimal authenticated payloads. Authenticate the destination, bound timeouts and
retries, persist deduplication keys, and distinguish queued, attempted, acknowledged
and failed delivery. A relay acknowledgment is not proof a human read an alert.
No configured/running notifier means no out-of-browser alert guarantee. Never send
keys, credentials, decrypted content or full fleet metadata in alert payloads.

Pause unaffordable discretionary uploads/deployments while preserving configured
application offline/security behavior. Receipt/data queues must have explicit byte,
item and age bounds and an owner-visible backpressure policy. Reject new work with
a durable reason when full; never silently discard existing entries or promise
unlimited retention. After confirmed funding, replay bounded pending work using
stable idempotency identities and checked effects, without duplicate apply or spend.

An expired binary cannot be reconstructed from Sui metadata. Restoration requires
a retained customer copy, digest verification, a verified re-upload/certification,
and an owner-authorized updated reference where the ID changes. Preserve identity,
confidentiality and decryption-scope checks through recovery.

## Persistence and acceptance still required

Sui records authority and references/accounting; it does not keep the referenced
binary available. Walrus retention is finite and depends on successful certified
storage and timely renewal. Seal/key providers have separate availability and
authorization requirements. Revocation cannot erase a copy already decrypted by a
recipient. Owners must retain and export verified artifacts, recipes, configuration,
necessary decryption recovery material and restoration instructions securely.

No implementation tests or live renewals are evidenced by this design. Before
Ready/Review, implement and pass all of these:

- Move reserve, authority, withdrawal, period-limit and concurrent-renewal tests;
  existing-object upgrade and old-package bypass checks on an isolated chain.
- Fake-clock/epoch boundaries, failed/uncertain checkout, low funds, changing quotes,
  executor/notifier outage, restart/race/duplicate recovery, quota/disk backpressure,
  alternate-wallet top-up and verified expired-blob restoration.
- Real executor/daemon/notifier integration and recovery drill coordination with
  [SF-091/#178](https://github.com/tyler-suifleet/suifleet/issues/178).
- Frontend typecheck/build and meaningful funding/storage compatibility tests after
  integrating the actual merged SF-083 implementation.
- Bounded dedicated-testnet renewal with chain/account/authority preflight, global
  mutation lock, checked effects/expiry, net costs and genuine exact-head CI.
- Fresh independent review. The implementation worker does not approve or merge.

Physical acceptance remains in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32), and
mainnet/production-provider/manual-wallet acceptance in
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Those checks are unrun. Their deferral does not waive software, isolated-chain,
automatic public-testnet, security or independent review gates. Platform tooling
continues to own OS installation, reboot and firmware recovery.
