# SF-188 CI funding reconciliation

On 2026-10-30, automatic testnet run [40974877135](https://github.com/tyler-suifleet/suifleet/actions/runs/40974877135)
failed on head `37d65978cf196248688e0475aefd188c31720661`, base
`9e22b599916bd838921eeb9cb98d643f133bca53`. The published
`testnet-effects-37d65978cf196248688e0475aefd188c31720661` artifact records:

- Verified chain `4c78adac`, protocol 139; checksum-pinned Sui 1.82.1 transaction CLI.
- Dedicated CI lane 1, address `0x0afccf110391cafb2844a986d20d6633436e2586c4e05e7c08fbc510dbfdcaec`.
- Whole-suite required balance 3,250,000,000 MIST; no held uncertain reservations.
- Initial balance 1,384,884,806 MIST. One successful faucet request credited
  1,000,000,000 MIST, leaving 2,384,884,806 MIST, still insufficient.
- No submitted transactions, zero allowance used, package allowance unchanged
  at 1,600,000,000 MIST. There is no application transaction to reconcile or replay.

The balance gate correctly stopped before setup. This was insufficient funding,
not a faucet failure or an enrollment regression. Do not weaken the gate,
reduce the suite coverage, replenish the allowance or repeat faucet calls in that run.

At approximately 00:09 UTC, a separate read-only check using the same verified
transaction binary (SHA-256
`de1d7effd53831af2863749befc35ec6df565b1fdfa8d8b2f5c866be23fa0d6b`)
confirmed chain `4c78adac` and found address balance 3,268,041,799 MIST plus
coin balance 78,358,890 MIST: total 3,346,400,689 MIST, above the requirement.
The intervening funding source was not determined; this worker did not fund it
and does not attribute those credits to a specific transaction.

The worker acquired the global network lease and coordinated in
[SF-010](https://github.com/tyler-suifleet/suifleet/issues/11#issuecomment-6511564829),
then released it promptly after discovering sufficient funds. No faucet request,
signing key access, transfer, publish, upgrade or app-object mutation was performed.
Only a public wallet configuration with an empty keystore was used for reads.

Local reproduction with `live_package_upgrade.funding_shortfall` rejects both
historical balances and accepts the newly observed pools at the unchanged
3,250,000,000-MIST requirement. Frontend budget component regressions,
`corepack pnpm --dir dapp typecheck` and `corepack pnpm --dir dapp build` pass
(17 static pages). `CHROME_EXECUTABLE=/opt/google/chrome/chrome node scripts/test_onboarding_browser.cjs`
passes on localnet and with `FLEET_TEST_NETWORK=testnet`; these use fixture
wallet/network responses and include the insufficient-budget race message. Node v24.21.0 and pnpm 12.14.0; the existing Tailwind
module-type warning remains nonfatal.

The snapshot base is already an ancestor of the owner branch and equals fetched
main; no additional base merge is needed. Enrollment source remains unchanged.
A new exact-head/current-base automatic testnet execution is still required:
this read proves funding availability only, not suite success. Hosted CI is
pending after the evidence push and is not watched by this worker. Independent
review remains required. No full Rust/Move/OTA suites or live application tests
were run in this repair. Manual wallet, production-provider, physical and mainnet
acceptance remain unrun and deferred to [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087](https://github.com/tyler-suifleet/suifleet/issues/159).
