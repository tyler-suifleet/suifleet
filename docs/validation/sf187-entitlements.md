# SF-187 implementation evidence — 2026-10-27

Chosen policy: registry-admin grants through Administration → Device entitlements.
Existing zero-limit customers can receive a grant; no contract API, layouts,
type origins or default entitlement changed. No contract migration/publication is
required. The frontend must be rebuilt/deployed through the normal reviewed path.

Local results:

- `sui move test --path contracts customer_tests` in a disposable network-none
  `suifleet-dev:latest` container (`sui 1.81.1-e04cf9aa915c`), with a private copy
  of the pinned framework cache and empty runtime wallet: 43 passed, 0 failed.
  Includes zero limit, cross-fleet boundary, successful grant/raise and refusal
  when a customer administrator lacks registry authority. Existing deprecation
  warnings remain. Initial container/config/cache setup failures were corrected.
- `node scripts/test_customer_entitlements.cjs`: passed. Production builders and
  fresh-state capacity helper cover zero, boundary, raised grant, u64 validation,
  allowance/period preservation, batch counts, registry binding and abort text.
- `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/test_customer_entitlements_browser.cjs`:
  passed. Actual component and SDK builder; fixture transport and wallet. Checks
  zero-limit guidance, registry-only grant visibility, invalid input and grant.
- `node scripts/test_customer_fleets.cjs`: passed.
- `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/test_customer_fleets_browser.cjs`:
  passed; generated historical screenshot restored, not included in this change.
- `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/test_onboarding_browser.cjs`:
  passed, including connect/rejection/session expiry/network mismatch and errors.
- `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/live_wallet_browser.cjs --smoke`: passed offline.
- `node dapp/node_modules/typescript/bin/tsc --noEmit -p dapp/tsconfig.json`: passed.
- `(cd dapp && node node_modules/next/dist/bin/next build)`: passed, 17 static
  routes including `/entitlements`. Existing Tailwind module-type warning remains.
- `python3 -m py_compile scripts/live_customer_entitlements.py`: passed.
- `git diff --check`: passed.

Dependencies were installed using `npx --yes pnpm@11.0.8 install --frozen-lockfile`.
Host Node was v24.21.0; no dependency/lockfile changes. A first attempt using a
stale dependency tree failed typecheck and was replaced by the locked install.

Testnet walkthrough:

[Sanitized transaction/effects/cost evidence](sf187-entitlements-testnet.json)
records chain `4c78adac`, the dedicated agent account, checksum-pinned CLIs,
verified UpgradeCap/type origins and cached fixture. No package publication or
existing customer/device edits. The mutation lock was acquired, asserted by the
runner, renewed and released; coordination is in SF-010/#11.

A fresh run-owned customer was refused at limit zero, then received a registry
grant of one. The production console helper checked real chain state and refused
before signing at zero, allowed after the grant, and refused at the one-device
boundary. First device registration executed successfully:
`SpzwTWN2Vf2dqdMziKUHJU1Wv4sBUS4chgUASgUuQwq`. An actual second registration at
the boundary aborted with `customer::enroll_checked` code 2. A registry grant of
two then made the console capacity check allow registration. Move tests also
exercise the second successful registration after raising the grant.

The initial runner underestimated object-storage gas budgets. Three exploratory
transactions failed with `InsufficientGas`; their effects/costs remain in the JSON.
Recovery reused the same private child identity and the original allowance. **No
second successful live registration is claimed.** The final runner scopes its live
checks to the first registration, boundary refusal and raised-grant capacity.
The registered test device was deleted and remaining child gas returned. The
empty run-owned customer/default fleet remain; there is no deletion API for them.

All 13 submitted transactions, including failures, are recorded. Net account spend
was **41,532,728 MIST (0.041532728 SUI)**. Conservative allowance usage was
**75,976,578 MIST**, below the original **80,000,000-MIST** ceiling, including
70,000,000 MIST total child funding. Refunds never replenished that allowance.
The run began with `scripts/live_customer_entitlements.py` and continued via
private `/tmp/sf187-resume.py` and `/tmp/sf187-cleanup.py` after inspecting failed
execution effects; the checked-in runner now uses adequate creation budgets.

Limits: browser fixtures are not a manual extension-wallet session; the production
frontend was not deployed here. Physical/mainnet/production-provider/manual wallet
acceptance remains unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). Hosted
exact-head/base ordinary/security CI, automatic Testnet CI and independent review
remain pending merge gates; none is represented by this local evidence.
