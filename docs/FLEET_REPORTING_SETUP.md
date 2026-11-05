# Per-fleet reporting setup

In **Fleet settings → Device reporting**, a current customer administrator can
set up reporting and storage in one wallet transaction. The transaction borrows
and returns the existing managed BudgetOwnerCap; current membership and all
organization protections still apply. A revoked or read-only credential cannot
set up or enroll reporting. Organizations requiring extra factors must use their
approved authenticated transaction path; this screen never bypasses policy.

The transaction creates FleetStorage and FleetData and adds a typed
`fleet_data::LocatorKey` dynamic field to the fleet budget. A failure rolls back
all three. Refresh readiness after any uncertain submission before retrying:
if the transaction succeeded, the existing locator is recovered. Duplicate setup
cannot overwrite the locator or silently replace existing device writers.

Each app release configures one reviewed package pair in
`NEXT_PUBLIC_FLEET_REPORTING_RELEASE`, a JSON object containing `packageId`,
`upgradeCapId`, `storagePackageId`, and `storageUpgradeCapId`. This release must
include `fleet_data::provision`, `publish_locator`, `LocatorKey`, and
`fleet_storage::new_storage` / `share_storage`, built against a core release with
`fleet_budget::attach`. Upgrade core, storage and data compatibly and
resolve the normal deployment manifest before enabling this release. Existing
public signatures, layouts and type origins are preserved. There is no per-fleet
build configuration: new fleets use the same static app and package release.

The resolver rechecks the deployment/chain, managed budget registry, both
packages and their UpgradeCaps/type-origin tables, the budget's typed locator,
shared data/storage objects and their registry/budget/storage/version bindings.
A query parameter, localStorage ID or arbitrary shared object is never locator
authority. Invalid bindings and read errors fail readiness; only genuine absence
permits an explicit legacy locator fallback. Historical LocatorKey introduction
origins are derived from the current verified package, not guessed from its call
ID. Storage versions 1–3 retain the same layout; older versions may still need
storage migration before archive management.

Explicit `NEXT_PUBLIC_FLEET_DATA` / `NEXT_PUBLIC_FLEET_DATA_CONFIGS` locators remain
a verified migration fallback when no authoritative locator exists. Their storage
introduction origin must be configured through `NEXT_PUBLIC_FLEET_STORAGE_ORIGIN`
(or the original `NEXT_PUBLIC_FLEET_STORAGE_PACKAGE`). To migrate without changing
writers or signed history, call `fleet_data::publish_locator` with the existing
FleetData, matching budget/current cap and FleetStorage; customer-managed callers
use the normal transaction-scoped capability loan. No initializer rerun or device
key/journal replacement is needed.

After setup, register/enroll the device in the fleet network budget, select it,
and choose **Enable device reporting**, then **Download device reporting
settings**. The download is an additive TOML fragment with the verified writer,
account and managed customer/fleet bindings. Merge it into the existing
`/etc/suifleet/config.toml` and install the matching `deployment.json`; retain the
existing device identity, signer configuration, keys, journals and component
state. Do not configure HTTP telemetry and FleetData simultaneously. An existing
writer with a replaced gas account must use the explicit account-replacement
flow rather than resetting its report history.

Verified readiness means configuration is readable and correctly bound. A device
with no signed report remains **unreported**. Health is independently verified
from its current registry signer and on-chain sample; downloading settings or
successful setup is not a health result. Storage starts empty: fund archive
storage through Network budget and configure the approved archive/Seal settings
before enabling encrypted log uploads. Health alone does not require a Walrus
purchase, provider credentials or a rebuilt app.

Local checks:

```sh
make test-fleet-reporting
make test-contracts
make edge-test
make test-ota
make dapp-typecheck
make dapp-build
# Disposable network-none chain with pinned 1.83.0 transaction/node CLI:
SF247_REPORTING=1 make test-fleet-data-upgrade-localnet
```

`scripts/live_fleet_reporting.py` automates the dedicated Testnet run: two fresh
managed fleets, dynamic discovery, unreported-before-report, frontend-generated
settings, actual daemon sample and independent browser signature verification.
It requires the held network lease, explicit dedicated wallet, pinned toolchain,
a built debug daemon/chain_facts example, and a non-replenishing allowance
(default 1 SUI, including failed transactions and all child/device funding).
Cached publications are content-keyed and reverified before reuse. Runtime
signing material stays in a private temporary directory; never commit it.
Physical Pi, actual Mainnet, production-provider and manual-wallet acceptance
remain unrun in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087](https://github.com/tyler-suifleet/suifleet/issues/159).
