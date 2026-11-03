# SF-200 review feedback repair

Addressed the independent review's blocking device-write/recovery finding.

Secured Customer arguments now encode the organization object ID across customer and storage wrappers. Current administrator checks, policy generation, exact operation arguments and other targets' existing snapshots/revisions remain enforced. Device-mutable `used`/`active_devices` no longer enter this Customer binding. The production console, queue reader and live fixture use the same encoding. Existing full-Customer intents require cancellation and requeue; OPERATIONS and the design note document that migration.

Added `delayed_recovery_survives_device_storage_charge`: enable delays, queue recovery, perform a real managed blob registration as an enrolled DeviceCap holder (which calls `reserve_device_allowance` and increases Customer.used), then execute the original recovery at maturity. Console tests also assert unchanged canonical intent and a non-stale queue after counter changes while retaining generation invalidation and tamper checks.

Verification on the final functional source:

- `sui move test --path contracts`: PASS 364/364.
- `sui move test --path contracts/fleet_storage`: PASS 80/80.
- `sui move test --path contracts/fleet_data --build-env testnet`: PASS 60/60.
- `make check-interfaces INSIDE_DEV_CONTAINER=1`: PASS compiled ABI, generated drift, negative ABI, wire vectors and client regressions.
- `node scripts/test_security_delay.cjs` and `node scripts/test_security_policy.cjs`: PASS.
- `cd dapp && pnpm typecheck && NEXT_TELEMETRY_DISABLED=1 pnpm build`: PASS.
- `git diff --check`: PASS.

Move/Next checks used the pinned `suifleet-sf164:protocol140` image. Compiler operations used network-none containers, cached Move dependencies and an empty temporary compiler keystore. No shared public-testnet wallet was mutated in this repair.

Size is still a combined-candidate gate: the first two isolated upgrades failed at 102900 and 102437 bytes. Their genuine failure receipts are retained. Factoring repeated identity and operation-name encoders preserves semantics and reduces package bytes; final isolated effects evidence follows below. These local tests do not establish the eventual combined candidate's package size.

Hosted CI/testnet for the pushed head remains pending and was not watched or polled. Fresh independent review and coordinator exact-head/base gates remain required. Manual/real-wallet, Pi, production-provider and mainnet acceptance remains unrun in #32/#159 (hosting #62). No approval, merge or deployment is claimed.

Final isolated validation: `python3 scripts/test_security_policy_localnet.py --suite live_security_delay --base /base/contracts --evidence docs/validation/sf200-feedback-localnet.json` PASS, 16 effects-checked transactions on disposable chain `a2e05f64`. Existing v197 Customer, compatible upgrade, old-package and legacy-timestamp refusals, cancellation, early refusal and mature recovery passed. Conservative allowance used 1600254796 MIST under the local 3000000000 ceiling; net gas 1586446296 MIST. All digests/type origins/verified unsigned package hashes are in the receipt. Source was compiled from the exact functional changes in `9a277944d492871460a0b20f9bd4e628701a0115`; the receipt's git-head field is the precommit parent because the test began before that commit. No functional edits followed compilation.
