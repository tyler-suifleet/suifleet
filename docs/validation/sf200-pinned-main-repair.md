# SF-200 pinned-main conflict repair

Source head before repair: `5edb5d5ae2b97469b764c570d8f7e096b122118e`.
Functional repair: `5fedb2d95e3c038e1ab0cf305df48648fa2868be`.
Conflicting integration base: `5b20af564124a2922315df139d748e3a9a2a0582`.

The coordinator reported a real conflict with that pinned base, not a failed
check on the original source head. Resolve that base into the owner branch;
no advancing-main freshness merge or rebase. SF-221's compact guards, private
symbols, constants, lifecycle and proof updates remain. SF-200's delay enforcement,
stable object-ID intent bindings, administrator-revision rollout snapshot and
multisig identity encoders remain. Availability is the union (multisig plus delay,
mask 9); other mechanisms remain unavailable. Public layouts and type origins
are unchanged. The five conflicted files were Customer, DeploymentRecord,
multisig_admin, security_policy and evaluate_move_prover.

The prover mutations now use SF-221's actual guard locations; the release
receipts-future guard remains an inline assertion. Its exact-once regression
is included. The live delay harness selects the exact policy abort function from
the compiled source before any chain operation: compact `p0` or legacy `check`.
It still requires the exact module, function and EFactor code from execution
effects; unknown/altered guard shapes fail closed. Three regression tests cover
current release, older inline guards and malformed/changed guard rejection.

## Verification

Pinned `suifleet-sf164:protocol140`, Sui 1.81.1 compiler, Rust 1.94.0,
transaction CLI 1.83.0; compiler and chain runs use `--network none`, uid 1001,
`sf200-move` compiler cache and temporary empty compiler keystores. Proof gates
use the private `/tmp/sf200-proof-repair/tools` cache mounted as `/proof-tools`.
No public wallet is mounted. Read-only chain_facts from the SF-089 worktree has
identical example source to this checkout; canonical Git metadata is read-only.

- `python3 scripts/check_move_security.py`: PASS, strict/unsuppressed compiler
  diagnostics, failing fixtures, 376 core / 82 storage / 60 data tests and all
  authority-bypass controls.
- `python3 scripts/check_move_proofs.py --tools /proof-tools`: PASS, 63 positive
  obligations and every expected negative control.
- `python3 scripts/check_move_native_proofs.py --tools /proof-tools`: PASS,
  supported positive/negative cases; UID reconstruction remains unsupported.
- `python3 scripts/check_move_registry_proofs.py --tools /proof-tools`: PASS,
  42 positive phase obligations and all five expected negative controls.
- `python3 scripts/check_move_object_proofs.py --tools /proof-tools`: PASS,
  21 positive phase obligations and every expected negative control; same-Name
  overlap remains explicitly unsupported, not proved.
- `python3 scripts/check_move_native_vm.py`: PASS, 19/19.
- `make check-interfaces INSIDE_DEV_CONTAINER=1`: PASS, compiled ABI, drift,
  negative ABI cases, wire vectors and client regressions.
- `node scripts/test_security_delay.cjs`, `node scripts/test_security_policy.cjs`,
  `node scripts/test_multisig_admin.cjs`: PASS.
- `CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/test_multisig_admin_browser.cjs`: PASS.
- `cd dapp && pnpm typecheck && NEXT_TELEMETRY_DISABLED=1 pnpm build`: PASS.
- `python3 -m unittest discover -s scripts/tests -p test_live_security_delay.py -v`: PASS, 3/3.
- `python3 -m unittest discover -s scripts/tests -p test_move_proof_mutations.py -v`: PASS.
- `python3 -m unittest discover -s scripts/tests -p test_build_move_prover.py -v`: PASS, 5/5.

All 54 secured Customer wrapper bodies were compared to the reviewed source,
normalizing only compact class constants, the binding guard name and the finish
alias: identical. `git diff --check` and the staged equivalent pass.

`python3 scripts/test_security_policy_localnet.py --suite live_security_delay --base /base/contracts --evidence /workspace/docs/validation/sf200-pinned-main-localnet.json`:
PASS, 16 effects-checked transactions on disposable chain `477ff1f9`. The base
was exported from the exact pinned integration base above. Publication and
compatible upgrade passed the actual package-size limit. Upgrade digest:
`GqJAhUEALnfdpqQayyENRc8DsAKinnn2dpVH7JLyzYBU`.
The [sanitized receipt](sf200-pinned-main-localnet.json) records all digests,
verified unsigned package hashes, type origins, authority checks, effects and
signed gas. Non-replenishing allowance: **1,506,881,596 / 3,000,000,000 MIST**;
net gas: **1,493,073,096 MIST**. Recompiling the committed contracts and
comparing `contracts_digest` to the fixture `head_hash` passes. Existing v197 object upgrade, all-off setup,
delay activation, old-package/direct weakening refusal, cancellation/consumed
nonce refusal, old queues without timestamps, early refusal, mature recovery
and restored all-off administration pass. Production-console payload hashes
match on-chain Action intents. Contract sources are identical to the repair
commit; the receipt's HEAD names the precommit parent because testing preceded
commit creation. The later harness-only edit moves guard-source selection before
preflight; its three unit regressions pass.

Initial attempts are not passing evidence: an unused compact constant and an
incorrect resolution of the rollout snapshot were found and corrected by the
compiler/tests. The initial interface container lacked its compiler cache/config;
the corrected configured run passed. Disposable-chain setup attempts encountered
a faucet-startup race and missing chain_facts. The first completed publication/
upgrade run stopped after six transactions because its exact expected abort
function was stale (`check` vs `p0`); both package effects succeeded, but that
run is not a delay-acceptance pass. Its receipt remains private at
`/tmp/sf200-pinned-main-abort-location.json` (chain `39c83014`, allowance used
1,483,067,861 MIST). The harness repair above addresses that finding.

## Remaining gates

Hosted CI and automatic public-testnet checks for the pushed head are pending,
not watched or polled. Fresh independent review and exact combined-candidate
CI/testnet effects/spend/publication-size acceptance remain coordinator gates.
An isolated-chain pass does not establish public-testnet acceptance.
No PR merge, other worker, new issue, public chain write/faucet, production service
deployment or hardware programming occurred.

Deferred and explicitly unrun: manual/real-wallet, physical Pi, production-provider
and actual-mainnet acceptance in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159);
production hosting remains SF-042/#62. This is a feedback repair, not approval or
complete acceptance. The coordinator owns integration.
