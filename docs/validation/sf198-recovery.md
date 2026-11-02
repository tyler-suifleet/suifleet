# SF-198 recovery evidence, 2026-11-01

Codex / gpt-6.1-sol; no finer runtime build exposed. Original claim, session
`dispatch-c81fa89e4e6a473cae8a970fd86dde45`, branch and worktree retained.
Draft [PR #451](https://github.com/tyler-suifleet/suifleet/pull/451) closes
[SF-198/#397](https://github.com/tyler-suifleet/suifleet/issues/397).

## Protected-run allowance repair, third recovery

Codex / gpt-6.1-sol (no finer runtime build exposed), same original claim,
session, branch and worktree. Repair commit `94870221c95d96abcdb88aa6f3579a8f3f6b684d`.
Inspected sanitized artifact 13111436498 (`package-upgrade.json`) from
[run 41374657219](https://github.com/tyler-suifleet/suifleet/actions/runs/41374657219),
which tested head d5e2dd25 against base 8ad43d7e in dedicated lane 1, chain
`4c78adac`. The five successful recorded effects charged 772,926,000;
8,425,378; 1,243,578; 784,577,363; and 8,737,163 MIST. Total allowance used
was **1,575,909,482 / 1,600,000,000 MIST**, before device funding. The single
faucet credit supplied wallet balance but did not replenish this allowance.
Native feature cases never ran. These artifact effects were inspected, not
independently fetched again from the public fullnode by this recovery.

The old “verified/reused base” diagnostic followed a **fresh publication** in
this run. It did not prove cache reuse. The completed pair and original device
identity were saved before the failure and the workflow persisted encrypted
fixture artifact 13111112721. This repair changes no contract bytes, so the
same base/head content key remains reusable; it must not publish either package
again. Cache reuse now also checks the UpgradeCap owner/latest package/version
and registry sharing before proceeding. A stale or missing object fails closed.

Core OTA preflight now checks existing device gas, the exact funding deficit,
the top-up reservation, inbox setup when needed, both deployment PTBs and
cleanup at full configured budgets **before any core-phase mutation**. This is
a conservative reservation bound, not a gas estimate or a claim that later
audit cases fit; those still reserve independently. If a fresh fixture exhausts
the allowance, its durable state remains for a subsequent bounded run. No
in-process ledger reset or automatic repeated run is introduced. Allowance
exhaustion is repair work, not a wallet funding failure that a faucet can fix.
Both device and passkey funding now transfer only the deficit. Existing passkey
gas is charged as a grant, closing the previous cached-gas accounting omission.
All ceilings, failed/unknown reservations, fixture keys and quarantine rules
remain unchanged.

Fresh verification:

- `docker run --rm --network none --user "$(id -u):$(id -g)" -v
  "$PWD:/workspace" -w /workspace -e INSIDE_DEV_CONTAINER=1 -e PYTHONPATH=/deps
  -v /usr/lib/python3/dist-packages/yaml:/deps/yaml:ro
  -v /tmp/sf196-network-183/sui:/opt/sui-network/sui:ro
  suifleet-dev:latest make test-testnet`: **pass**, 23 rollout + 177 testnet +
  67 shared-live + 3 passkey tests, authenticated cache and receipt-effect Node
  suites; pinned installed-toolchain assertion passes. Regression drives
  `run()` through completed-fixture reuse with zero signing/publication,
  reproduces the exact failed-run arithmetic, checks full-reservation boundaries,
  deficit-only funding and charging retained gas. Mocked negative-path cleanup
  diagnostics are expected test output, not live activity.
- `node scripts/test_passkey_fixture.cjs` and
  `node scripts/test_passkey_factor.cjs`: **pass**.
- `python3 -m py_compile scripts/live_package_upgrade.py
  scripts/live_passkey_factor.py` and `git diff --check`: **pass**.

Initial regression attempts exposed the old funding-class expectation and
incomplete offline CLI/Walrus test doubles (invalid derived address, missing
publisher); corrected before the final complete suite. No failure was ignored.
The reported green ordinary/security CI applies to the prior d5e2dd25 head;
new-head hosted checks remain pending after push and are not watched or polled.
Actual native public-testnet setup/allowed/refused/recovery evidence and fresh
source acceptance are still required. Keep PR #451 **draft**, claim reserved
and **not Review**. The existing protected CI lane owns the signing state;
this recovery did not decrypt it, create/rotate keys, request a faucet, mutate
public chain state, republish code or acquire a mutation resource. SF-173
publisher and SF-181 USDC blockers are untouched. Deferred manual/hardware/
provider/mainnet checks remain unrun in #32/#159. Historical evidence below
remains scoped to its original commands and revisions.

## Repair

Preserved unfinished source changes in 3b1d8ee2, then reconciled the quarantined
owner repair 44d677aab557 through merge 2e157009. The direct private-file read,
ENOENT-only generation and exclusive creation remain; malformed-file diagnostics
also conceal file content. Saved redirect-16 index/worktree/remote refs remain
unchanged. All commits use Tyler Bush as both author and committer.

Shared scoped authority setup and canonical BCS string literals reduce bytecode
without removing public functions, layouts or authority checks. Compiled root
modules total 93,469 raw bytes (16 modules); real package admission and compatible
upgrade now pass. Independent SDK BCS vectors check all 53 compact labels.

The protected CI security suite now calls native passkey acceptance using the
package gate's exact cached fixture and its existing 1.5-SUI ledger. It verifies
UpgradeCap owner/version/latest head and passkey type origin; it never republishes
packages. Keys and uncertainty markers use the existing encrypted CI fixture
state. No operator wallet, faucet or spending allowance was added. Fresh expensive
fixtures may exhaust the unchanged suite allowance; that is a failure, not a
waiver. Preserve caches and reconcile uncertainty rather than republishing.

## Verification actually run

Commands ran in `suifleet-dev:latest`, network none, host UID/GID, checkout at
`/workspace`; Move checks additionally mount the warm Move cache. Chain tests
mount the canonical Git metadata read-only, `/tmp/sf198-base/contracts` at
`/base/contracts`, the checksum-pinned 1.83.0 network CLI and built `chain_facts`.
The base is the exact pre-feature contracts from 02bca2dd.

- `sui move test --path contracts`: **359/359 pass**.
- `python3 scripts/generate_interfaces.py --check`: **16 modules pass**.
- `python3 scripts/check_move_security.py`: **pass**, strict linters,
  unsuppressed diagnostic checks, root359/storage79/data60 tests and actual
  failing authority mutations.
- `make test-customer-fleets` with `INSIDE_DEV_CONTAINER=1`: **pass**, SDK signing,
  tamper/binding/recovery/browser/transport negatives, private-file subprocess
  regressions, policy/customer suites, real Chromium console regressions and
  wallet bundle smoke check. This is automated software evidence, not a manual
  extension-wallet/hardware-authenticator walkthrough.
- `cd dapp && pnpm typecheck && pnpm build`: **pass**, static production export.
- `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p
  'test_testnet*.py'`: **175 tests pass, one skip**.
- `PYTHONPATH=scripts/tests:scripts python3 -m unittest test_live_passkey
  test_testnet test_live_common`: **115 pass**. An initial invocation named the
  nonexistent `test_testnet_live_common` module; corrected to `test_live_common`.
- Python syntax compilation, Node syntax check and `git diff --check`: **pass**.
- `python3 scripts/test_security_policy_localnet.py --runner
  scripts/live_passkey_factor.py --base /base/contracts --evidence
  docs/validation/sf198-recovery-localnet.json`: **pass, 35 effects-checked
  transactions**. The receipt contains package IDs, split type origins, all
  digests and per-transaction net gas. Both standalone and cached CI adapter
  cases ran against the actual disposable node: possession/enrollment, policy,
  allowed wallet+passkey action, refused wallet-only and old-package calls,
  backup-key recovery, removed-key refusal, surviving-key action and disable.
  Allowance used **1,730,358,233 / 1,800,000,000 MIST**, including grants.

The first repeated cached-adapter attempt failed before submission because a
reused passkey coin fell below the 20M full reservation. Raising its top-up
threshold from 15M to 30M fixed it without rotating identities or increasing the
allowance. Final receipt reports starting Git head 893042ec with that one-line
runner correction present; the correction is committed in 5fa041eb. Contract and
frontend sources match those tested; later changes only record this evidence.
Historical failed receipts remain historical and are superseded only for the
specific local checks now passed.

## Outstanding gates

Explicit read-only agent-wallet `active-address` still returns `None`. No public
wallet was created or rotated and no public mutation or faucet call was attempted.
The automatic protected lane is wired for native feature validation, but its
public-testnet effects are **not yet observed**. Push requests fresh exact-head
ordinary CI/CodeQL through the PR event; hosted results remain pending and are
not polled. Local CodeQL CLI is unavailable; scanner remediation is not claimed
verified by CodeQL. Keep PR draft and claim reserved pending genuine public
feature evidence and remaining review/security gates. No Review/approval/merge
is claimed. No shared resources remain held.

Manual real-wallet/hardware-key, physical, provider and mainnet acceptance remain
unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159). Their deferral does
not waive software/security/public-testnet gates.

## Exact-head CI repair, second recovery

Codex / gpt-6.1-sol (no finer runtime build exposed), original claim/session.
Inspected actual job 125038218940 logs and sanitized artifact 13108877670
from run 41369972588 at head 8b8cc9c7. Four blocking HIGH
`js/file-system-race` findings were in `scripts/test_passkey_fixture.cjs`
(lines 12, 14, 16, 17), not the production fixture generator. Repair 10af9125
uses one opened descriptor for fstat/read/truncate/write assertions and directory
contents for the read-error assertion. No finding filter or gate changed.
The actual ordinary check job 125038218941 also failed isolated discovery with
`ModuleNotFoundError: live_passkey_factor`; the test now establishes its scripts
import path like the neighboring standalone tests.

Fresh checks: `node scripts/test_passkey_fixture.cjs`,
`node scripts/test_passkey_factor.cjs`, Node syntax and `git diff --check` pass.
`env -u INSIDE_DEV_CONTAINER make test-testnet RUN=` passes on host (toolchain
installation check skipped); full `make test-testnet` in network-none
`suifleet-dev:latest` with host UID, checkout at /workspace, INSIDE_DEV_CONTAINER=1,
PYTHONPATH=/deps, host `/usr/lib/python3/dist-packages/yaml` mounted at /deps/yaml
and existing `/tmp/sf196-network-183/sui` mounted at /opt/sui-network/sui passes:
23 rollout + 175 testnet + 67 common + 2 passkey tests, authenticated cache and
receipt-effect Node suites. The pinned installed-toolchain assertion passes.
Initial host run with INSIDE_DEV_CONTAINER=1 failed for absent sui; cached-image
attempts failed for missing PyYAML and a mistakenly mounted 1.81.1 CLI; these
were environment failures, corrected by the explicit mounts above. An extra
misnamed `node scripts/test_passkey.cjs` invocation failed before execution;
the actual `test_passkey_factor.cjs` suite passed.

Pushing this repair requests ordinary CI and, on successful originating CI, the
existing protected automatic native-feature testnet lane. No hosted waits or
polling, machine-wallet creation, limit increase, fixture deletion or public
transaction was performed. The local CodeQL CLI remains unavailable; fresh
CodeQL and genuine native public-testnet effects remain unverified. The claim
stays reserved and PR draft until actual required acceptance is ready. Missing
machine active-address is not the blocker to this CI route. Independent feature
review remains required; no approval, Review readiness or merge is claimed.
