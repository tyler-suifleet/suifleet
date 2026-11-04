# SF-198 pinned-main combination repair

This repairs PR #451 against `a0bffea59276a1c92e07cd2cf70a3b2846ff6aeb`.
Repair merge `8f7f8742b6976331b113e9eb1e51ecf8a750f872` preserves both
parents. Original source `c21be3b76979021241a5362706ab9a7728e17db1` remains a merge
parent/ancestor; the stopped worker’s private `attempted-resolution.patch` is
preserved. The original working tree was clean. The retained resolution was
104,993 bytes, above the unchanged 102,400-byte package limit.

The final package is **102,393 / 102,400 bytes**, a reduction of 2,600 bytes.
Only **7 bytes remain**. Both independent offline size implementations agree,
and actual compatible-upgrade execution succeeds on the disposable chain.
Future root-package changes must pass the size gate before signing.

The additional compaction renames **package-visible**, non-entry helpers and
updates their callers; original names remain in `Package helper:` comments.
These functions are excluded from the reviewed public ABI. The pinned Move
compatibility checker checks removal of externally public/entry functions;
package helpers are internal linking details. All externally public signatures,
struct layouts/abilities and introduction origins remain intact. An inverse
rename/token comparison against the restored pre-compaction combination passes
for every root module: no implementation statement changes beyond the helper
identifiers. The tiny passkey constant-pool adjustment uses the same u64 values
without repeated casts. No authority check, numeric abort, limit, suppression,
proof assertion, spending allowance or execution-effects check is removed.

The conflict resolution retains subscription authority/version 201 alongside
all optional security factors. Three new tests use a real subscription purchase
to establish that enrollment/recovery still work, an existing native passkey
policy survives purchase, and wallet-only action remains refused after the seal.
The scenario must explicitly restore wallet-owned gas after a sponsored purchase;
its context builder otherwise retains sponsorship across transactions.

Exact abort selection now follows package-helper aliases and main’s existing
selector guards. Head-package factor refusals use the exact source-selected
function/module/code, including the native passkey, multisig, second-person and
delay runners. Older-package checks keep their own source. Proof overlays and
mutation targets follow the renamed package helpers; only the changed overlay
hash pins are regenerated. Makefile conflict resolution retains both branches’
regression suites. The handover guide documents its required base-source argument.

## Commands and results

Docker commands used image `suifleet-sf221:localtest`, `--network none`, uid/gid
1001, the checkout at `/workspace`, private dependency cache
`/tmp/sf198-private-move`, pinned compiler Sui 1.81.1 and transaction CLI Sui
1.83.0. The CLI and existing `chain_facts` helper were mounted read-only. No
public wallet was mounted. `INSIDE_DEV_CONTAINER=1` selected direct Make targets.

- `make test-contracts`: **PASS**, 468 root + 82 storage + 60 data tests.
- `sui move test --path contracts passkey_factor_tests`: **PASS**, 23 tests,
  including the three subsequently added subscription/passkey regressions.
- `python3 scripts/check_move_security.py`: **PASS**, strict compiler/linters,
  both size calculations, unchanged unsuppressed compatibility baselines, all
  610 Move tests and genuine failing authority-bypass mutations. No scanner
  success is substituted for checking its diagnostics.
- `make check-interfaces test-testnet`: **PASS**, size/compiled ABI/generated
  interfaces and frontend interface regressions; 337 offline Python testnet/
  selector/plumbing cases plus authenticated-cache and receipt-effects Node
  checks. Mock transaction/funding messages in these suites are offline fixtures.
- `python3 scripts/check_move_proofs.py --tools /tmp/sf169-proof-tools`:
  **PASS**, 63 positive phase obligations and all 27 exact negative controls.
  Model errors/timeouts do not count as negative proofs.
- `python3 -m unittest discover -s scripts/tests -p test_build_move_prover.py`:
  **PASS**, five tests. `test_move_proof_mutations.py`: **PASS**, all selected
  controls mutate exactly their intended statement and refuse repeat mutation.
  Selector, delay-location and passkey unit regressions also pass.
- `make test-customer-fleets dapp-typecheck dapp-build`: **PASS**, native SDK
  passkey/tamper/payment/metadata cases, optional-factor console fixtures,
  Chromium entitlement/multi-fleet regressions, TypeScript and static export.
  Browser fixtures do not establish real extension-wallet/authenticator support.
- `python3 scripts/test_security_policy_localnet.py --runner
  scripts/live_passkey_factor.py --base /base --evidence
  /workspace/docs/validation/sf198-pinned-main-passkey-localnet.json`: **PASS**,
  23 effects-checked transactions, with `/base` exporting the exact pinned-main
  contracts. [Sanitized receipt](sf198-pinned-main-passkey-localnet.json).
  Upgrade digest `AF98Q6PiSjRpeA1AJHftwFXwnpqTxqMtdieMxoAwK84p` succeeds.
  Setup, wallet+passkey allowed action, wallet-only refusal, old-package refusal,
  backup recovery, removed-key refusal and final disable/removal pass. Net local
  spending is 1,664,004,247 MIST; conservative non-replenishing allowance charged
  1,732,132,025 / 1,800,000,000 MIST. All IDs/type origins are in the receipt.
- `python3 scripts/test_protocol_localnet.py --customer-upgrade`: **PASS**,
  actual populated-object upgrade, adoption/reconciliation, exact old-package
  refusals, wallet revocation, real daemon managed receipt, onchain rollout,
  explicit reassignment and removal. [Sanitized effects](sf198-pinned-main-customer-localnet.json)
  contain 30 successful transaction digests and the distinct refusal effects.
- `python3 scripts/test_second_person_localnet.py --base /base --evidence
  /workspace/docs/validation/sf198-pinned-main-second-person-localnet.json`:
  **PASS**, 26 effects-checked transactions, with `/base` exporting `35f1f80a^`
  (the actual pre-second-person framework). Distinct signed approval, exact
  execution, self/substituted/old/current bypass refusals, pending-index reads,
  rejection and factor-authorized recovery pass. [Sanitized receipt](sf198-pinned-main-second-person-localnet.json).
  Net local spend 1610856415 MIST; conservative allowance charged
  1649448599 / 1,800,000,000 MIST.
- `git diff --check`: **PASS**.

The frontend build permission failure was repaired by changing ownership only of
this worktree’s ignored generated `dapp/.next` and `dapp/out` directories, using
an isolated root container with only those two directories mounted. Tracked
sources, dependency permissions, global caches and other worktrees were untouched.

Intermediate experiments are not passes: narrow-constant and arithmetic-helper
experiments did not recover enough space and were discarded; shortening
`enroll_internal` triggered the compiler’s intentional self-transfer diagnostic
and was discarded without adding suppression; the initial new wallet-only test
retained the scenario’s sponsor and was corrected to restore wallet gas.
An extra second-person run with the current-main base stopped after four valid
transactions because its fixture intentionally requires a pre-introduction base;
its origin assertion correctly refused that unsuitable fixture. Its raw receipt
is retained privately; it is not counted as second-person acceptance.

## Remaining acceptance

These are local software results, not public-testnet acceptance, a deployment or
independent review. Successful exact-head/current-base hosted ordinary/security
CI, protected automatic public-testnet native setup/allowed/refused/recovery and
upgrade effects/spend evidence, and fresh independent review remain mandatory
coordinator gates. Hosted CI was not watched or polled. No public mutation,
faucet, additional wallet, issue, worker, merge, deployment or hardware operation
occurred in this recovery. The localnet resource lease is released after the
local nodes exit.

Manual wallet/authenticator, physical Pi, production-provider and actual-mainnet
checks remain **unrun**, retained in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
