# SF-198 integration with the pinned SF-199 base

This repairs the actual PR #451 conflict between source `4c9c8bfe` and pinned
candidate base `c45c1fc635343b30a390ab466ba76311b3bfa83a` (merged SF-199).
The original source head did not fail these checks. No advancing main was used.

The resolution preserves both optional introduction-origin groups, generated
interfaces, console controls and the indexed second-person action lifecycle.
Available factors are 1/2/4; delay remains unavailable. Passkey enrollment accepts
both authority 197 and sealed authority 199. Capability loans require the native
signature over the whole PTB and still refuse queued second-person grants.
Combined Move regressions exercise enrollment/recovery after a real proposal seals
authority, native sponsorship plus package-only independent attestation, and
refusal when either required factor is absent. The package-only test attestation
is a kernel fixture; the actual independent signer is tested by the separate
second-person isolated-chain harness.

Commands ran in `suifleet-dev:latest`, with `--network none`, the checkout at
`/workspace`, a warm private Move cache, checksum-pinned transaction CLI at
`/opt/sui-network/sui`, and the existing `chain_facts` helper. No public wallet,
faucet or mutation was used. Compiler 1.81.1; transaction CLI 1.83.0.

- `python3 scripts/generate_interfaces.py`: passed, both reviewed ABI additions
  retained; compiled ABI matches the generated clients.
- `make test-contracts`: 388 core, 79 storage, 60 data tests passed.
- `make check-interfaces`: 1,314 references / 19 modules, zero drift; generator
  check and generated-interface tests passed.
- `make test-customer-fleets`: passed, including second-person/passkey/multisig
  fixtures, Chromium multisig and multi-fleet flows, and offline wallet smoke.
- `make test-testnet`: passed (27 rollout, 212 testnet, 73 shared-live, 5 native
  passkey Python tests, plus authenticated cache and receipt-effects Node checks).
- `make dapp-typecheck dapp-build`: passed, including prerequisite regression
  suites and static production export.
- `python3 scripts/test_security_policy_localnet.py --runner
  scripts/live_passkey_factor.py --base /base --evidence
  /workspace/docs/validation/sf198-sf199-localnet.json`: passed from an export of
  the exact pinned candidate's contracts, with 23 effects-checked transactions.
  Upgrade digest `CtZjSWVXEJsktPpmvorjHXCbt7HDwFR2C9Fupgb1gvv4`;
  bytecode digest `1e94e369591d324cb2ff7a1467dfe3536e6dfe1ff49dc1062544d2780f27d1cc`.
  Net disposable-chain spend 1,594,636,247 MIST; non-replenishing allowance charged
  1,662,764,025 MIST. The [receipt](sf198-sf199-localnet.json) records setup,
  native signed allowed/refused/recovery effects, reuse, and actual type origins.

- `python3 scripts/test_second_person_localnet.py --base /base/contracts
  --evidence /workspace/docs/validation/sf198-combined-second-person-localnet.json`:
  passed, 26 effects-checked transactions including distinct signed approvals,
  old/current bypass refusal, exact-argument execution, rejection and recovery.
  Net disposable-chain spend 1,587,303,612 MIST; allowance charged
  1,625,872,999 MIST. [Receipt](sf198-combined-second-person-localnet.json).
- `python3 scripts/check_move_security.py`: passed warnings-as-errors compilation,
  negative compiler fixtures, authority tests and genuine failing bypass mutations.
- `git diff --check`: passed. Rust daemon unit/simulator tests were not rerun;
  its generated ABI remains checked by the compiled-interface gate.

Initial validation setup errors were corrected: the host lacks `sui`; an uncached
container could not fetch dependencies offline; the second-person runner's first
base argument pointed at a repository root rather than its contracts directory.
The first correctly configured second-person run reached a real failed-effect
refusal but its historical expectation named private guard `p0`; the combined
native passkey verifier aborts at `security_policy::check`, still code 1. The
runner now requires that exact function/module/code and still checks failed
execution effects. No failed effect is relabeled successful.

Hosted exact-candidate ordinary/security CI, automatic public-testnet native
passkey acceptance and fresh independent review remain coordinator gates. No
hosted CI was watched or polled. Manual wallet/authenticator, physical target,
provider and actual-mainnet acceptance remain unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [#159](https://github.com/tyler-suifleet/suifleet/issues/159).
This is a repair receipt, not final merge approval.
