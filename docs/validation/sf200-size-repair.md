# SF-200 combined package size repair

Functional source: `9667ddea980e183a1540d522344fc900f604beae`.
Pinned multisig base: `df5d6d8005801034a75d4ce4180dcf713d82efd8`.

This repairs the actionable `MovePackageTooBig` finding in the independent review
of `e45472d188d83c939f4618addcbd34f7f1a53b76`. The historical failing receipt
in [the composition report](sf200-multisig-composition.md) is retained.

`customer::begin_intent` shares the canonical operation/Customer-ID prefix and
administrator check across 54 secured wrappers. Each wrapper supplies the same
ordered operation-specific tail, class and pause flag. Tail construction is
read-only; authority is checked before granting or executing any action. Repeated
abort paths in Customer and DeploymentRecord share private helpers. Abort codes,
public/package signatures, layouts, abilities and intent bytes are preserved.
The enrollment-limit assertion retains its original function location for the
existing exact-abort acceptance harness. Other factored failures retain their
module/code but report the private helper as the abort function.

The 16 production modules total **92,731 bytes**, down **2,426 bytes** from
95,157. Customer is 30,225 bytes; DeploymentRecord's module is 16,800 bytes.
This is module bytecode, not the complete package object size. The successful
actual upgrade below establishes the complete package fits the protocol limit.
It remains close to that limit; future combined candidates still need actual
publication/upgrade validation rather than a module-byte estimate.

## Final checks

All commands used `suifleet-sf164:protocol140`, `--network none`, Sui compiler
1.81.1 and transaction CLI 1.83.0. Move/interface runs used uid 1001, cached
`sf200-move` dependencies and an empty temporary compiler keystore. Frontend
checks used the container root user for existing root-owned Next build output.
No public-testnet wallet or production identity was mounted.

- `sui move test --path contracts`: **376/376 PASS**.
- `sui move test --path contracts/fleet_storage`: **82/82 PASS**.
- `sui move test --path contracts/fleet_data --build-env testnet`: **60/60 PASS**.
- `make check-interfaces INSIDE_DEV_CONTAINER=1`: **PASS**, including compiled
  ABI/client references, generated drift, negative ABI cases, wire vectors and
  client regressions.
- `node scripts/test_security_policy.cjs`: **PASS**.
- `node scripts/test_security_delay.cjs`: **PASS**, including queue readers,
  canonical payload hash, countdown/cancel, tamper/transport and stale failures.
- `node scripts/test_multisig_admin.cjs`: **PASS**.
- `CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/test_multisig_admin_browser.cjs`:
  **PASS**, actual Chromium fixture wallet/transport component tests.
- `cd dapp && pnpm typecheck && NEXT_TELEMETRY_DISABLED=1 pnpm build`: **PASS**.
- Compared all public/package function declarations and struct headers in both
  changed modules to the pre-repair head: **unchanged**.
- `git diff --check` and staged equivalent: **PASS**.

## Exact-source isolated upgrade

`python3 scripts/test_security_policy_localnet.py --suite live_security_delay --base /base/contracts --evidence /workspace/docs/validation/sf200-size-repair-pinned-localnet.json`:
**PASS**, 16 effects-checked transactions on disposable chain `4689b785`.
The base was exported with `git archive df5d6d8005801034a75d4ce4180dcf713d82efd8 contracts`.
The receipt's source HEAD is the exact functional commit above. Compiler 1.81.1
built the bytecode; transaction CLI 1.83.0 verified and signed it. The runner
verified chain/account, UpgradeCap ownership/version, type origins and effects.
Upgrade digest: `D8DREYndcNaRkgK7ZATpQg1xyj1Zdckyt5HHv8kKKdBW`.
Conservative non-replenishing allowance used: **1,614,196,396 / 3,000,000,000 MIST**;
signed net gas: **1,600,387,896 MIST**.

The [sanitized receipt](sf200-size-repair-pinned-localnet.json) records all
transaction digests, gas, effects status, verified unsigned package hashes and
type origins. It covers existing-v197 compatible upgrade, all-off setup,
delay activation, old-package/direct weakening refusals, cancellation/consumed
nonce refusal, immutable creation timestamp, early refusal, mature recovery,
legacy queue rejection and restored all-off administration.

Exploratory runs also passed from the older framework base and from the pinned
multisig base before the final helper-name/abort-location cleanup. They are not
substituted for the committed exact-source receipt above. Final Move/interface
checks were rerun after that cleanup. No failing final check was ignored.

## Remaining gates and limitations

Hosted exact-head/current-base CI, funded automatic public-testnet acceptance
and fresh independent review remain required and pending/unverified at this
repair handoff. No hosted checks were watched or polled. The prior CI lane funding
shortfall is separate from this repaired source defect; no success is fabricated.
This is a repair receipt, not merge approval or complete acceptance.

No public-testnet writes/faucet, main merge, service deployment, hardware
programming or other model workers. Manual/real-wallet, physical Pi,
production-provider and actual-mainnet acceptance remains unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159);
production hosting remains in SF-042/#62. The coordinator owns the next
independent review and gated integration.
