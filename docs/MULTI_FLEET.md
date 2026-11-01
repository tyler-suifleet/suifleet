# Multiple named fleets per customer

SF-136: [issue #280](https://github.com/tyler-suifleet/suifleet/issues/280).
The implementation adds shared `customer::Customer` and `customer::Fleet`
objects. A wallet is a revocable credential; the Customer object ID is the
stable identity. No indexer or additional always-on service is required.

## Authority and inventory

A Customer records its display name, current credentials, administrators,
fleet inventory, default fleet and aggregate entitlements. A Fleet records its
customer, alias, exact budget and device/group/deployment/policy inventories.
Aliases may collide: selection persists object IDs, while the switcher displays
customer and fleet names and exposes IDs in details.

`customer::create(registry, name)` atomically creates one default fleet and its
empty budget. A registry marker keyed by the initial credential makes retries
idempotent, including after an unknown transaction outcome. Revoking that
credential does not erase the marker or create a replacement customer.
Additional `create_fleet` calls never change entitlements or usage.

Administrators manage every fleet of their customer. Read-only credentials
can discover inventory but cannot borrow budget authority, administer policies,
move devices or decrypt owner archives. Owned, non-transferable Membership
objects are discovery hints; clients re-read Customer credentials and verify
registry/customer/fleet bindings. Explicit FleetData support grants also discover their configured managed fleets without granting customer administration. Revocation takes effect at contract admission,
including for retained Membership objects. The last administrator cannot be
removed. Authorize and verify a replacement before revoking an old credential;
the Administration page supports this without moving the customer or its caps.

Fleet custody holds budget, device-owner and policy-admin capabilities. Budget
capability loans have an unstoreable hot potato and must return the same cap to
the same fleet in the same transaction. This composes the reviewed SF-130/131
settings API, funding, storage and data owner writes without handing a wallet a
persistent cap. Device and policy caps are not lent to arbitrary callers.

Groups organize devices **within** a fleet. Managed group admission, deployment
and rollout targets, desired-state revisions, private policy grants and receipt
reporting check the exact fleet inventory. An administrator of two fleets cannot
insert one fleet's device/group into the other. Immutable catalog versions remain
explicitly shared, authenticated inputs. Catalog publishing authority stays with
the catalog publisher; customer membership does not confer publisher authority.
Administrators change many memberships at once with the bounded batch entry
points described in [bulk group membership](GROUP_BATCH.md).

FleetData, storage, gas accounts and authenticated archive identities keep their
existing budget/storage/registry bindings and layouts. Explicit support grants
and policy account grants stay independent of customer discovery. Membership
alone does not silently add support access. Managed archive reads use the new
read-only `fleet_data::seal_approve_customer` entry point: Seal approval PTBs
contain only approval calls and read-only shared inputs, not capability loans.
Original FleetData IDs, key epochs and ciphertext identities are preserved. Support diagnostics are separately revalidated and restricted to one exact request for a current device of the selected fleet.

## Enforced legacy boundaries

Older packages remain callable. Adoption establishes these concrete barriers:

| Object | Managed boundary | Older entry points |
|---|---|---|
| Customer / Fleet | Authority version 1, checked at membership, issuer and fleet admission | Future explicit migrations can retire these methods without changing layouts |
| Budget | Version 2 plus package-created managed marker; cap in fleet custody | Version-1 code refuses mutations/draws; current legacy enrollment/removal APIs refuse managed budgets |
| Device | Owner cap replaced during adoption and held in fleet custody | Every other legacy account bound to the old cap stops passing registry identity checks; the selected account is explicitly rebound |
| Group | Stored admin becomes the fleet object address | Wallet callers cannot satisfy the original admin check |
| Deployment | Creator becomes fleet address; customer binding and receipt authority version 3 | Old report code refuses newer receipts; current legacy report APIs also refuse managed records |
| Claim policy | Operator is fleet address; legacy `open` remains false; separate managed active marker | Old enrollment aborts closed; wallet-based policy administration cannot impersonate the fleet |
| Access policy | Admin cap remains in fleet custody | A revoked wallet cannot retain or supply it to older grant APIs |

New daemon reporting validates managed custody and the current fleet. Contract
receipt admission additionally checks the active signer, current device fleet,
record creator, inventory and target/group membership. A device reassigned later
cannot write a source fleet's managed receipt through current or old APIs.

## Customer allowances

New customers receive **zero** device and encoded-upload allowances. Creating
another customer or rotating a wallet therefore cannot mint a subscription.
Only the registry's entitlement authority can call `set_entitlement`; customer
administrators cannot grant themselves capacity. There is no subscription
collection/payment backend. An eventual billing integration must verify the
customer identity and grant its allowances through that authority, rather than
trusting browser input, a wallet address or a budget deposit as payment proof.

`active_devices` counts assigned/enrolled devices, including suspended or revoked
ones until explicit removal. Adoption reserves the existing budget count before
any cap is reconciled. Managed `customer::remove_device` returns the released
`DeviceOwnerCap`; a PTB must consume or explicitly transfer that result. The
wallet builder transfers it to the connected administrator. Removing a device
releases customer capacity and leaves historical deployments and archives in place.
The new customer API uses mutable transaction contexts for future upgrade
compatibility. Registration, sponsored claims, removal and atomic reassignment
update customer and fleet accounting together. Each fleet has its
own gas/storage balances, per-device quotas, settings and retention funding.

The customer upload allowance is measured in Walrus **encoded bytes**, shared
by managed owner and device uploads across all fleets/storage objects. Period
IDs advance only under entitlement authority and cannot move backwards; changing
fleets or credentials cannot reset usage. A device still must pass its fleet's
quota, active identity, account and funding checks. Charges and storage
registration commit atomically. A rejected upload cannot consume allowance.

The issuer also calls `fleet_storage::configure_customer_charging(customer,
registry)` once for the reviewed storage producer. A private non-store witness
binds device charges to that producer's type origin, preventing a device from
calling the ledger directly to burn the customer allowance outside storage
quotas. Configuration is idempotent for that origin. Storage upgrades preserve
it. For a pre-existing paid period, grant only its verified remaining allowance;
no migration/browser path asserts that billing has been implemented.

## Browser and daemon configuration

The switcher keys preferences by chain identifier, registry and credential and
persists fleet IDs. Queries also include the resolved account/network/customer/
fleet identity. Reconnects use a fresh discovery generation. Stale explicit
selections never silently fall back to another fleet. Discovery errors,
revocation, disconnects and mismatched networks hide previous content. Cross-tab
storage events revalidate preferences. Changing identity remounts page forms,
archive plaintext and sessions. Mutations re-read membership, verify the network,
and check the captured selection immediately before wallet signing.

Managed device/group/deployment, health/alerts, access-policy, budget/settings,
storage, onboarding and claim-enrollment paths use the selected fleet. Storage
is resolved by its exact budget. The settings form continues to use
`fetchFleetSettings(client, budgetId)` and
`buildSetFleetSettingsTx(budgetId, capId, settings)` through the capability loan.

For multiple existing data objects, `NEXT_PUBLIC_FLEET_DATA_CONFIGS` is a JSON
array of the existing schema-1 FleetData configuration records described in
[SERVERLESS_FLEET_DATA.md](SERVERLESS_FLEET_DATA.md). Each budget has at most one configured record.
The selected budget chooses its record; a missing record never falls back to
another fleet or wallet-scoped HTTP telemetry. The original single-record
`NEXT_PUBLIC_FLEET_DATA` remains supported for legacy manifests and a matching
managed budget. Readers verify chain, registry, budget and storage on chain.

A daemon's enrollment is explicit and never changes when the browser switches.
Generated managed claim provisioning files add:

```toml
[managed]
customer_id = "0x…"
fleet_id = "0x…" # customer::Fleet, not fleet_data::FleetData
budget_id = "0x…"
```

For managed archive production, the existing `[fleet_data]` configuration adds
`[fleet_data.managed]` with `customer_id` and `fleet_id`. Keep the original
FleetData/storage/writer/account IDs and type origin. Managed storage registration
uses this binding and the same bounded, verify-before-sign policy as legacy
registration. `suifleet --check-operator CONFIG` reads and verifies the complete
managed custody/customer/registry chain without submitting a transaction.

## Explicit migration and recovery

Keep a release inventory before any writes: chain/registry, all introduction
origins, owner caps, budget accounts, groups, deployments, data/storage IDs,
support grants, key epochs and ciphertext references. Follow
[DEPLOYMENT_MANIFEST.md](DEPLOYMENT_MANIFEST.md) and [OPERATIONS.md](OPERATIONS.md).
Retain the previous manifest while preparing the compatible core, storage and
data upgrades. New data/storage call packages must use the reviewed managed
APIs and preserve the original external type origins.

1. Create/read the Customer and default fleet. The `CustomerCreated` event and
   registry `CustomerKey { wallet }` resolve an uncertain result; retrying create
   cannot duplicate it. Have entitlement authority grant verified capacity and
   configure the storage charging producer before managed device uploads.
2. Under current ownership, call `adopt_budget(customer, budget, ownerCap, name)`.
   This consumes the cap, reserves the existing device count and creates a fleet
   with `pending_devices`. Recover by finding the budget in Customer's fleet
   inventory; never create another fleet just because a wallet no longer owns
   the consumed cap. Loans/new enrollment stay sealed while devices are pending.
3. For **each indexed account**, call `adopt_device(customer, fleet, budget,
   account, registry, deviceOwnerCap)`. It verifies the exact indexed account and
   current identity, replaces the owner cap, rebinds only that account and records
   custody. Account IDs, reserves, limits, signer keys and journals remain.
   Recover from current Fleet device inventory, `pending_devices` and registry
   identity events. A consumed cap cannot be adopted twice. Missing/transferred
   caps can instead be explicitly retired by a customer administrator using
   `retire_pending_device(customer, fleet, budget, account)`. Use this for stale
   accounts after legacy deregistration, duplicate enrollment already adopted by
   another fleet/customer, or lost/destroyed capabilities. Because capability loss
   cannot be proven on chain, an administrator may deliberately retire any still
   indexed, unadopted account; inspect its ID before signing. This permanently
   closes that account, returns its reserve to this budget, and decrements pending
   and customer active counts exactly once. It does not delete the registry
   identity, another fleet's assignment or historical archives. Adopted devices
   require the normal group-aware removal flow. Repeat retirement is rejected;
   recover uncertain results from the budget index and account closed flag.
   Once every indexed account is adopted or retired, capability loans and budget
   withdrawals/settings resume. Re-enrollment is a separate authorized operation.
4. Adopt groups with `adopt_group` only after all their members are assigned to
   the same fleet. For SF-125 indexed groups, finish `index_members` under the
   existing wallet admin first, then call `adopt_indexed_group(customer, fleet,
   group, members)` with the complete distinct member IDs from the index.
   The contract checks the member count, every membership and every fleet
   assignment before handing over authority; omitted, duplicate or foreign
   members abort atomically. `adopt_group` rejects hidden indexed members.
   Managed add/remove/delete operations preserve the index and device accounting.
   New managed groups retain vector membership; the legacy wallet-only index
   migration control is hidden for them. Managed deployments/rollouts continue
   using the daemon's bounded event scan: legacy inbox announcement functions
   require wallet-owned caps/admin and cannot impersonate a managed fleet.
   Existing legacy fleets keep SF-125 inbox announcements and index migration.
   Adopt native/rollout records with `adopt_deployment` after
   verifying all targets. This preserves their payload, controls and authoritative
   historical receipts while sealing future reporting. Catalog-bound legacy
   deployments must be superseded with a managed deployment of the immutable
   version and an explicit desired-state revision; do not change publisher
   provenance. Existing unadopted objects retain legacy semantics.
5. Adopt private policies only when all their device/group grants fit the fleet.
   Account/support grants are explicit and retained. Replace legacy enrollment
   policies with managed policies; never repurpose unconsumed claims silently.
   Record which claims/policies were closed and which were replaced.
6. Update explicit daemon bindings and current call packages, preserving device
   keys, journals and component state. Verify `--check-operator`, settings,
   signer-bound reporting, uploads and archive approvals. Test old-package
   refusals; an upgrade alone is not evidence that old packages stopped working.

`rename_fleet` and `set_default_fleet` are explicit administrative operations.
Removing or reassigning a device requires clearing its group and direct policy
grants first. `reassign_device` is atomic within one customer: close/reclaim the
source gas account, retain device key/custody, create a destination account with
zero initial float/reserve and preserve the aggregate count. Source deployments,
archives and receipt history stay in the source fleet. Update the daemon's explicit budget/account/data configuration after a move; use FleetData's explicit account replacement API when retaining a destination writer's history. `DeviceReleased`,
`DeviceAssigned` and registry identity events preserve the authority history.

## Verification and acceptance

Changed-behavior tests live in:

- `contracts/tests/customer_tests.move`: authority, claims, counters, aliases,
  reassignment, cross-fleet targets/policies and sealed legacy paths.
- `contracts/fleet_storage/tests/fleet_storage_tests.move`: populated adoption,
  customer-byte charges, quota refusal and forged producer witnesses.
- `contracts/fleet_data/tests/fleet_data_tests.move`: unchanged archive identity
  with read-only customer approval and credential-role rejection.
- `scripts/test_customer_fleets.cjs`: actual SDK PTB capability composition and
  chain-read fixtures for discovery, stale credentials and binding failures.
- `scripts/test_customer_fleets_browser.cjs`: actual React provider/switcher and
  Chromium account/network/tab/race/selection isolation. The
  [screenshot](validation/sf136-fleet-switcher.png) is this deterministic software
  fixture, not a manual extension-wallet or public-network acceptance result.
- `scripts/test_protocol_localnet.py --customer-upgrade`: disposable network-none
  base-to-head upgrade, populated adoption, old-package failures and real daemon
  custody resolution. Temporary keys stay private and are removed.

Exact [software validation results](validation/SF136.md) and sanitized local-chain effects are recorded separately. The pushed head belongs in the PR/issue handoff. Hosted CI,
automatic public-testnet checks and independent exact-head/base review remain
required integration gates. Physical/mainnet and final provider/manual-wallet
acceptance remain unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [#159](https://github.com/tyler-suifleet/suifleet/issues/159), under the standing
implementation/acceptance split. A screenshot or simulator is not those results.

A revoked initial wallet cannot create a replacement customer by retrying
onboarding: the initial registry mapping is permanent. Ask a current
administrator to restore access or grant membership; retrying Create is
idempotent. Managed enrollment policy closure is permanent; create a replacement
policy to resume claims.
