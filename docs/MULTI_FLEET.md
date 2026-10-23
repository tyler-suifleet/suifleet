# Multiple fleets per customer (SF-136)

Status: source audit and proposed integration design, **not implemented**.
SF-136 is [#280](https://github.com/tyler-suifleet/suifleet/issues/280).
This document does not establish customer membership, fleet isolation, billing,
migration safety or UI acceptance. The existing single-wallet application still
runs unchanged.

## Source audit

Audited baseline: `ab621e7`. A fleet alias must not be confused with a group name,
a wallet address, the package deployment manifest or the registry singleton.

| Area | Existing boundary | Work needed for SF-136 |
|---|---|---|
| Customer identity | No persistent customer object or customer-to-wallet membership index exists. | Introduce a stable customer identity, revocable credentials and a discoverable fleet inventory. |
| Budget | `contracts/sources/fleet_budget.move`: each shared `FleetBudget` has its own `BudgetOwnerCap`, registry, device index, balance, limits and version gate. `enroll_device` checks this budget's cap and the device's current owner cap. Multiple budgets already exist independently. | Bind each managed budget to one customer/fleet; maintain authoritative customer-wide enrollment accounting. A count in one budget cannot enforce a customer-wide allowance. |
| Storage | `contracts/fleet_storage/sources/fleet_storage.move`: each `FleetStorage` binds one budget; owner/device operations check that binding. Upload quotas and retention balances are storage-local. | Preserve bindings and retention authority; additionally account for any configured customer-wide allowance. Creating another storage object must not reset that allowance. |
| Data | `contracts/fleet_data/sources/fleet_data.move`: `FleetData` binds budget, storage, registry and chain; writers bind fleet/device/account. Enrollment and account replacement validate the budget index. Multiple independent FleetData objects are possible. | Discover the correct tuple per selected fleet. Preserve support grants, key epochs and archive identity; membership in a customer alone must not silently confer support/decryption authority. |
| Devices | `contracts/sources/device_registry.move`: shared registry, custodied devices, transferable device owner caps, signer identities and revocation; no customer/fleet binding. | Add authoritative single-fleet assignment for managed devices. Keep device identity and history stable; require an explicit authorized reassignment transaction. |
| Groups | `contracts/sources/device_group.move`: group has an admin address and device IDs. `add_device` checks the group admin, not customer/fleet membership. | Separate groups from fleets; bind managed groups and every inserted device to the same fleet. A name or matching wallet is insufficient. |
| Deployment | `contracts/sources/deployment_manager.move`: records have a creator and target ID dynamic fields; construction accepts target IDs without a customer/fleet binding. Receipt authorization separately validates targeting. | Bind managed records to a fleet and validate all target objects at authoritative admission. Reject cross-fleet targets even when one wallet administers both. |
| Delegation | `contracts/sources/roles.move`: revocable group-scoped deployer roles, attributed to an operator address. | Preserve that scope; customer membership must not widen existing roles. Resolve revoked credentials at each use. |
| Catalog | `contracts/sources/component_catalog.move`: publisher/catalog provenance is separate from device/group ownership. | Catalog versions may be explicitly shared immutable inputs. Deployments, desired-state revisions, private policies and budgets remain fleet-owned; catalog visibility must not imply deployment or decryption authority. |
| Manifest | `docs/DEPLOYMENT_MANIFEST.md`: network/package/type origins and shared registry describe a deployment, not a customer. | Keep introduction origins and registry identity unchanged. Add optional discovery origins only when implemented; do not substitute the current package ID for old origins. |
| Browser inventory | `useDevices`, `useGroups`, `useDeployments` query wallet-scoped inventory with network/account query keys. | Add verified customer/fleet selection to queries and to every mutation's pre-sign checks. Clearing a menu alone does not isolate cached data or pending transactions. |
| Browser budget/storage | `useFleetStorage` and `components/FleetBudget.tsx` select `budgets.data?.[0]`. | Resolve the selected fleet's exact budget/cap/storage tuple. Never fall back to the first budget after a failed selection lookup. |
| Browser health/logs | `lib/fleet-data.ts` parses a single `NEXT_PUBLIC_FLEET_DATA`; `chain-alerts.ts` uses that global configuration. Health/alerts hooks also support wallet-scoped HTTP telemetry. | Pass verified selected-fleet configuration through health, archives, alerts, diagnostics and access policies; bind HTTP authorization and caches as well as chain queries. |
| Onboarding | `components/FirstRun.tsx` registers/enrolls for the connected wallet; `lib/onboarding.ts` derives progress from wallet inventory. | Create/select the customer's one default fleet atomically and idempotently before enrollment. Retry after an unknown transaction outcome must read authoritative state. |
| Daemon | `src/sui_client.rs` resolves the current device owner from its registry owner cap and checks deployment creator/group authority. `src/config.rs`, `src/funding.rs` and `src/fleet_data.rs` separately configure budget/data bindings. | Teach managed enrollment/admission about stable fleet membership and authorized customer credentials without weakening legacy checks. UI switching never changes daemon enrollment. |
| Wallet handover | `docs/FLEET_HANDOVER.md`: recipient-confirmed, typed inventory; transfer is resumable, not globally atomic. | Preserve legacy owner/support authority and historical objects; credential rotation must not require moving the customer's identity or resetting allowances. |
| Billing | Existing prepaid gas/storage is not a subscription billing service. | Define allowance administration and verified customer identity separately from collection. Do not describe deposits or a browser price estimate as subscription enforcement. |

## Settings interface coordination

SF-130 [#265](https://github.com/tyler-suifleet/suifleet/issues/265) and SF-131
[#266](https://github.com/tyler-suifleet/suifleet/issues/266) own the settings paths.
The following unmerged snapshots have incompatible exports from the same file:

- SF-130 `5b0c5ff0571d046479914ee7dba51b9c2ee6613f` exports tuple-valued
  `SETTINGS_BOUNDS`, `DEFAULT_SETTINGS`, `settingsErrors`,
  `buildSetFleetSettingsTx(budgetId, capId, settings)` and
  `fetchFleetSettings(client, budgetId)` from `lib/fleet-settings.ts`.
- SF-131 `91f6cdc68482a5417c48e304ee870a39f3506dc3` exports object-valued
  `SETTINGS_BOUNDS`, `validateSettings` and pricing/estimator helpers from that
  same path. `lib/fleet-settings-tx.ts` still returns a disabled `settingsSaver()`.
  PR [#277](https://github.com/tyler-suifleet/suifleet/pull/277) is open.

Required integration agreement: retain SF-130's settings dynamic field on the
selected `FleetBudget`; keep price/estimator presentation separate from wire
validation; pass the selected budget/cap explicitly into reads and saves. Do not
copy settings into Customer or assume either unmerged export shape is canonical.
The owners must reconcile these exports and the saving adapter before SF-136
changes the shared settings/budget selection flow. The source audit above does
not depend on that merge and has been completed independently.

## Proposed authority and discovery design

This section is a proposal for the implementing worker and shared-interface
owners, not an agreed ABI or an instruction to migrate existing objects now.

Use an additive shared Customer object with stable object identity, an authority
epoch, credential roles, one default fleet ID, fleet inventory and entitlement
accounting. Fleet aliases need not be globally unique. Discovery must use a
chain-verifiable wallet-to-membership index or owned membership credentials;
each candidate must be checked against current Customer state so a stale or
transferred credential cannot grant access. Decide how initial customer identity
is issued and recovered; permitting each new wallet to self-assert the same paid
customer, or to reset a paid allowance with a new Customer, is not enforcement.

Use an additive named-fleet object with immutable customer binding and verified
budget/storage/data references. Capability custody and the existing owner/support
checks remain authoritative until an explicit migration changes their handling.
Multiple wallet credentials cannot safely borrow a wallet-owned budget cap merely
because the UI lists them as members: implement package-controlled custody or a
separately reviewed delegation boundary. Define least-privilege membership roles
and preserve a recovery authority before revoking the last administrator.

Track active managed devices once at Customer scope and once at fleet scope.
Enrollment/removal/reassignment updates both atomically; repeated requests cannot
increment or release capacity twice. Customer diagnostic/log allowances need one
period ledger across fleets and credentials, with monotonic period identity.
Per-device/per-fleet limits may tighten this allowance. Serialize only operations
that need shared accounting; preserve read-only fleet-budget inputs on ordinary
gas draws where possible. Specify who can grant/change entitlements without
inventing subscription collection or trusting browser-supplied limits.

Managed operations must carry and validate the complete customer/fleet/object
binding at contract admission, receipt verification, daemon admission and Seal
policy resolution. An isolated module wrapping old entry points is insufficient:
old packages remain callable. In particular, adding a group membership table
does not stop an older `add_device`, and adding a customer counter does not stop
an older `enroll_device`. Use an enforceable migration/version/custody barrier
and prove old-package bypass rejection before describing managed isolation as
implemented. Legacy single-fleet objects can retain their old semantics until
explicit adoption; do not silently claim they gained managed guarantees.

## Browser integration contract

Selection identity is `(chain identifier, registry, customer ID, fleet ID)`;
local preference additionally keys on the connected credential. Persist IDs,
not aliases. Revalidate membership and object bindings after reconnect, account
or network change, transaction completion and cross-tab storage notifications.
Hide previous data synchronously while identity is unresolved. Treat loading,
empty inventory, revoked/stale selection and discovery failure as distinct states.
Do not automatically substitute another fleet when a stale operation is pending.

Capture selection/credential/network for each mutation and recheck after every
asynchronous proof and immediately before signing. Include them in cache keys,
invalidate dependent queries on change and discard late responses from a prior
selection. Contract checks must still reject a forged transaction. Give the
switcher an accessible label and keyboard interaction; show aliases primarily
and IDs in optional details, including when aliases collide.

## Migration and recovery requirements

No migration command exists yet; do not execute a hypothetical migration based
on this proposal. The implementation must provide a verified, resumable plan:

1. Read and record chain, registry, package/type origins, all selected owner caps,
   device identities, groups, budgets, data/storage references and support grants.
   Refuse mixed-customer or foreign inventory before writes.
2. Create/read the customer's default fleet with an authoritative idempotency
   marker. Adopt each object only once under its current legitimate authority;
   do not infer membership from matching wallet addresses or names.
3. Establish the old-package barrier and reconcile aggregate active devices and
   outstanding allowance usage before enabling managed writes. Persist progress
   on chain so a retry after an unknown outcome reads instead of duplicating.
4. Preserve original archive fleet identity, key epoch, type origin and ciphertext
   references. Keep historical decryption grants subject to existing policy;
   rotating a credential cannot retract previously disclosed plaintext.
5. Regenerate compatible client manifests, update explicit daemon bindings and
   verify mixed clients/old entry points. Preserve device keys, journals, component
   state and receipt history. A UI fleet change never reassigns a device.

## Required evidence before Review

No functional tests have been run for SF-136 because no implementation is present.
The eventual implementation must supply:

- Move tests for two fleets/one customer, separate customers, duplicate aliases,
  multi-wallet access, rotation/revocation, cross-fleet group/target/writer/storage
  rejection and aggregate allowance bypass attempts.
- Actual isolated-chain existing-object adoption and upgrade tests, including
  calls through old package IDs, interrupted migration and idempotent retry.
- Generated ABI/BCS checks, Rust tests/build and OTA simulator coverage for managed
  admission, legacy regression and explicit reassignment/history behavior.
- Browser tests for default-fleet retry, creation/switching, concurrent tabs,
  account/network changes, revoked selection, cache/late-response isolation and
  pre-sign selection changes, plus end-to-end UI evidence.
- Frontend typecheck/production build, current-head CI, applicable automatic
  dedicated-wallet public-testnet validation and independent source review.

Physical target/mainnet and final production-provider/manual-wallet acceptance
remain unrun and tracked in [#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [#159](https://github.com/tyler-suifleet/suifleet/issues/159). Those deferrals do
not waive any software/security/regression gate listed above.
