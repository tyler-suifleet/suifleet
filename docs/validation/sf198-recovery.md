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

## Native payment and interrupted-setup repair, fourth recovery

Source repair: `fa483aa9f04425057ae3acfd669d5f47613787d5`.

Codex / gpt-6.1-sol (no finer runtime build exposed), same claim/session/branch.
Inspected artifact **13114533516** and the failed-job log from
[protected run 41383243081](https://github.com/tyler-suifleet/suifleet/actions/runs/41383243081),
head `059dafaa`. Package reuse passed at **121,060,916 MIST**. Native
possession proofs and wallet enrollments both succeeded. Policy preparation
then exited before recording a digest; upgrade-security conservatively retained
its 20,000,000-MIST unknown reservation and finished at **1,167,696,523 /
1,500,000,000 MIST**. The historical subprocess stderr was captured privately
by Python but **not exposed in the log or effects artifact**; it cannot honestly
be reconstructed as an observed historical message. See the sanitized
[receipt](sf198-native-protected-failure.json).

Independent read-only gRPC effects confirmed enrollment, funding and final device
sweep. The operator's remaining coin (28,241,408 MIST) came from that final sweep;
its address balance was 2,985,633,898 MIST. A read-only differential using the
same policy/customer and a 30,000,000-MIST construction budget reproduced old
stderr `fixture gas coin unavailable`; the repaired builder produced unsigned
bytes. This changes no on-chain policy and proves no public acceptance. The
original failing policy budget was 20,000,000 MIST, not the differential's 30M.

The fixture now resolves native address-balance payment with a chain-bound
`ValidDuring` expiration and random nonce, or bounded/paginated coin payments.
It retains exact budget/sender/sponsor checks and offline final serialization:
full gRPC simulation would stop expected refusals before validators execute.
Preparation failures expose only an allowlisted diagnostic, with no retry or
release of unknown reservations. Missing-factor failures must match
`security_policy::check` abort 1, rather than merely returning failed effects.

The next protected run would also have failed by enrolling the same keys again.
Setup now reads the existing factor binding, validates its BCS shape and
administrator/key identities, and reuses proved enrollments. Unexpected bindings
fail closed. No key rotation, fixture replacement, state clearing, publication,
spending-limit change, workflow-authority change or automatic retry is added.
Contract bytes are unchanged; reuse the existing exact protected package pair.

Verification commands (run from this worktree):

- `node scripts/test_passkey_payment.cjs`: wallet/sponsor, coinless address balance,
  replay binding, fixed budget, no simulation, fragmented/paginated coins and
  insufficient/malformed gas negatives.
- `node scripts/test_passkey_fixture.cjs` and `node scripts/test_passkey_factor.cjs`:
  private-file/identity/secret-safe failure and SDK native signing/tamper suites.
- `PYTHONPATH=scripts/tests:scripts python3 -m unittest test_live_passkey`:
  **5 pass**, retained identity/uncertainty, deficit grants, existing BCS bindings,
  corruption and subprocess redaction without retries.
- Network-none `make test-testnet`, with the same image/PyYAML/pinned transaction
  CLI mounts documented above: **23 + 177 + 67 + 5 tests pass**, plus Node
  authenticated-cache and independent-receipt suites.
- Node/Python syntax and `git diff --check` pass.

The local regression uses the following exact container invocation; the
`chain_facts` binary is the existing local build, not a new daemon change:

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD:/workspace" \
  -v /home/tyler/dev/codex/src/suifleet/.git:/home/tyler/dev/codex/src/suifleet/.git:ro \
  -v /home/tyler/.move:/home/dev/.move \
  -v /tmp/sf198-base:/base:ro \
  -v /tmp/sf196-network-183/sui:/opt/sui-network/sui:ro \
  -v /tmp/sf196-edge-target/debug/examples/chain_facts:/opt/chain_facts:ro \
  -e SUIFLEET_CHAIN_FACTS=/opt/chain_facts \
  -v /usr/lib/python3/dist-packages/yaml:/deps/yaml:ro -e PYTHONPATH=/deps \
  -w /workspace suifleet-dev:latest \
  python3 scripts/test_security_policy_localnet.py \
  --runner scripts/live_passkey_factor.py --base /base/contracts \
  --evidence docs/validation/sf198-address-balance-localnet.json
```

Final result: **PASS, 23 effects-checked transactions**, allowance used
**1,714,804,025 / 1,800,000,000 MIST**, including repeated conservative grants.
The [actual receipt](sf198-address-balance-localnet.json) records exact digests,
per-transaction costs, existing-object upgrade/type origins, two reused enrolled
identities, successful coinless policy setup with `ValidDuring`, allowed action,
wallet-only and removed-key `security_policy::check` abort 1, old-package refusal,
backup recovery, surviving-key action, disable and final removal. The cached
adapter reused this exact local pair; it published no additional package.

Initial local attempts failed for missing facts-binary/git/framework-cache mounts,
a deliberately oversized self-transfer, a coinless-payment assertion before
adding address funds, and the CLI's base64 binding representation. A further
attempt passed the resumed native sequence but exhausted the unchanged allowance
while starting a redundant second full sequence. The final harness instead
performs enrollment, interrupts, and resumes through the actual cached adapter
once; it keeps all feature cases and the original 1.8-SUI local ceiling. No failed
attempt is represented as a passing run.

Public native allowed/refused/recovery effects and independent final acceptance
remain outstanding. PR #451 stays draft and this reservation stays Blocked;
new-head hosted CI is requested by push and must be collected by the existing
coordinator, without worker polling. No protected state was decrypted or public
transaction submitted. Deferred manual/hardware/provider/mainnet checks remain
unrun in #32/#159.


## Current-main integration and source-review recovery, 2026-11-02

Codex / gpt-6.1-sol (no finer runtime build exposed), same original claim,
session, branch, worktree and PR #451. This section supersedes earlier draft /
Blocked handoff instructions **only for source-review readiness**, under the
explicit user recovery direction. Public native effects remain a final merge
gate; they are not an API development dependency or a passed acceptance check.

Merge `1c5f4c4d1194e7c46dd3f181cf05e286871be16d` integrates fetched main
`3fbf160e25d010ee88f976feb90b2cbbd3f04579` with preserved feature head
`b0d3df076171a800a6f85c63e9a9e8d6f873c4ee`. Main changes only coordinator
code/docs/tests; merge has no conflicts and no contract/frontend/signing-state
changes. Tyler Bush is both author and committer. The earlier local note naming
cf65ea17 as this merge base was incorrect: that was the protected run's base.
The actual tested merged base is 3fbf160e, as recorded by the merge parents.

Inspected already-completed ordinary CI and CodeQL at b0d3df07: success, including
[CI run 41388586121](https://github.com/tyler-suifleet/suifleet/actions/runs/41388586121).
Inspected the completed failure log of
[protected run 41392636950](https://github.com/tyler-suifleet/suifleet/actions/runs/41392636950):
base cf65ea17, head b0d3df07, chain 4c78adac, primary CI lane 0.
Five recorded package transactions consumed **1,575,909,482 / 1,600,000,000
MIST**; core OTA preflight required 280,000,000 MIST with only 24,090,518
remaining. It stopped before native feature cases. No native public acceptance
is inferred from the successful package upgrade or historical lane-1 enrollment.
This measured shared cost failure is tracked in existing
[SF-221/#463](https://github.com/tyler-suifleet/suifleet/issues/463), separately
owned. No issue, allowance, wallet, faucet, signing state, unknown reservation,
fixture publication or workflow authority was changed by this recovery.
No hosted wait/watch/poll loop was used.

The runbook and design still incorrectly described the repaired package-size
failure and local-only adapter as current blockers. Updated their status to
separate passing isolated implementation evidence, independent source review,
and still-unproved public native feature acceptance. No release is claimed.

Fresh combined-base validation uses network-none suifleet-dev:latest, host UID,
checkout /workspace, INSIDE_DEV_CONTAINER=1, PyYAML mounted at /deps/yaml with
PYTHONPATH=/deps and pinned /tmp/sf196-network-183/sui at /opt/sui-network/sui.
Move cache is a private copy /tmp/sf198-private-move mounted at /home/dev/.move.
Initial Move and isolated attempts failed because the shared cache's Git index
was root-owned; a frontend attempt without its cache failed resolving GitHub
inside network-none. These are failed environment attempts, not passed tests.
The shared cache was left unchanged; the copied cache is host-user owned.

The fresh isolated invocation is the fourth-recovery Docker command above with
that private cache mount and output
`docs/validation/sf198-current-main-localnet.json`: **PASS, 23 effects-checked
transactions**, allowance used **1,714,804,025 / 1,800,000,000 MIST**. It compiles
the pre-feature base and current contracts, performs actual compatible upgrade
of existing objects, verifies split type origins, interrupts/resumes enrollment
with the same two keys and executes coinless setup, allowed action, wallet-only /
removed-key aborts, old-package refusal, backup recovery, surviving-key action,
disable and final removal. Local node identity/keys are disposable and never
public-wallet identities. Receipt records every digest and net cost. Resource
build:suifleet-localnet held only during use and released after node exit.

Fresh `make test-testnet`: **PASS**, 23 + 177 + 67 + 5 Python tests and Node
cache/receipt suites. `make test-contracts`: **PASS**, root359/storage79/data60.
`make check-interfaces`: **PASS**, 16 compiled modules and ABI/negative/recipe
checks. Remaining combined source/frontend/coordinator results follow below.

Final combined commands/results:

- Container `bash -ec 'make test-contracts; make check-interfaces; make
  test-customer-fleets; make dapp-typecheck; make dapp-build'`: **exit 0**.
  This includes SDK passkey signing/tamper/binding/recovery, private fixture and
  coin/address-balance negatives, actual Chromium console regressions, frontend
  TypeScript and Next.js 16.4.1 static production export. No manual wallet or
  hardware-authenticator result is implied.
- Host `python3 -m unittest discover -s tools/pr-coordinator/tests`: **685 tests
  pass**, exit 0. Mocked failure/worker diagnostics and ResourceWarnings appear
  in this offline suite; final result is OK. No model workers were launched.
- `git diff --check` and structural relative-file link checks of changed
  operational/design documents: **pass**.

Only the coordinator integration, corrected guidance and fresh evidence changed
in this recovery; no new feature/API change was needed after inspection and
combined validation. Original source/payment/size repairs are retained, not reset
or replaced. The runbook status correction is the concrete remaining source
repair. No unrelated dirty/staged work existed at entry or was discarded.

PR #451 is now handed off Ready for independent **SOURCE** review with the
original claim published Review, following the explicit recovery authorization.
Review is not Done, source approval, public native acceptance or merge approval.
New-head ordinary/security/current-base CI and protected public-testnet native
setup/allowed/refused/recovery are **pending/unproved**; the existing coordinator
must enforce them before merge. SF-221/#463 owns shared cost repair separately.
No dependency_wait or false dependency is introduced. Manual wallet/hardware,
Pi/provider/mainnet remain unrun in #32/#159. No resources remain held. All new
commits retain Tyler Bush as author and committer. The worker pushes once and
exits without watching hosted CI or starting another worker.

## Merged SF-221 engineering recovery, 2026-11-02

Codex / gpt-6.1-sol (no finer runtime build exposed), original SF-198 session,
claim, branch and PR #451 retained. Entry had no dirty or staged work. Merge
100f75fe incorporates actual published main
5b20af564124a2922315df139d748e3a9a2a0582 (PR #465), including its native
multisig integration. This supersedes the earlier instruction to wait for
SF-221 source. SF-228 retains its separately owned budget/funding work.

Conflict resolution preserves the merged compact Move guards, narrow literals,
shared secured wrappers and intent encoding, native multisig enrollment/UI/tests,
and funded identity reuse. Available factors are now the union 3 in Move and
TypeScript; the verifiers combine with AND. Passkey sponsor/admin bindings,
possession proof, current-factor rotation and last-key refusal remain intact.
The reviewer's suggested registered-second-administrator negative is now a
committed Move regression; it aborts in security_policy with code 1. Expired
enrollment cleanup remains a nonblocking storage-hygiene observation, not a new
implementation commitment or a repaired feature.

A concrete integration defect remained in the retained SF-198 preflight: it
summed old lifecycle reservations before the merged compact lifecycle could
recycle predecessor storage. For the measured SF-221 larger-candidate fresh
receipt, 42,640,611 MIST remains before funding. The old 260M gate would refuse
despite the compact path's initial 35M requirement (10M deficit + 10M funding
budget + 15M first deployment budget). The compact preflight now uses that
selected initial reservation; every later call independently reserves its
**entire configured budget before signing**, without assuming any rebate.
Noncompact behavior is retained. This does not guarantee the remaining calls
fit, raise a limit, reset a ledger or release an unknown reservation. Cached
device gas is charged and recorded as preexisting_device_grant_mist. Regression
covers this exact historical boundary, one-MIST-below refusal and cached 30M
gas requiring 45M. Actual measured receipt replay separately verifies fresh and
cached ledgers, failed fresh versus successful cached outcomes, retained
identities, full reservations, unknown outcomes and non-replenishing faucet/
rebate behavior. No identical public package was republished.

Fresh local commands use the Docker invocation in the address-balance section
above, substituting the private cache mount
/tmp/sf198-private-move:/home/dev/.move and INSIDE_DEV_CONTAINER=1.
All containers have --network none and the pinned 1.81.1 compiler / 1.83.0
transaction CLI; PyYAML and chain_facts mounts are unchanged.

- make test-testnet: PASS, 27 rollout + 211 testnet + 67 shared-live + 5
  passkey Python tests, plus authenticated-cache and independent-effects Node
  suites. The pinned installed-toolchain check ran. Fresh/cached SF-221 receipt
  replay and the new compact preflight boundary are offline, not new public
  cost measurements.
- make test-contracts: PASS, root 370/370, storage 79/79, data 60/60.
- make check-interfaces: PASS, compiled ABI/layouts, generated interfaces and
  its frontend protocol/recipe regressions.
- make test-customer-fleets: PASS, passkey SDK signing/tamper/binding/recovery,
  private fixture permissions/identity preservation, coinless payment,
  multisig SDK and real Chromium component tests, fleet/browser regressions
  and offline wallet bundle smoke.
- make dapp-typecheck and make dapp-build: PASS, TypeScript and Next.js
  16.4.1 static production export.
- python3 scripts/test_security_policy_localnet.py --runner
  scripts/live_passkey_factor.py --base /base/contracts --evidence
  docs/validation/sf198-cost-merged-localnet.json: PASS, actual compatible
  existing-object upgrade and native cached-adapter interrupted-enrollment
  recovery, 23 effects-checked transactions. The
  [receipt](sf198-cost-merged-localnet.json) records every digest, origin and
  fee: 1,607,767,047 MIST net spent; conservative allowance used
  **1,675,894,825 / 1,800,000,000 MIST**, including grants. Setup, allowed,
  wallet-only refusal, old-package refusal, removed-key refusal, backup
  recovery, surviving-key action, disable and final removal all executed.
  Both cached enrolled keys were reused and policy preparation used coinless
  address-balance payment. This is disposable local-chain evidence only.
- git diff --check: PASS. Local mutation resource released after node exit.

Initial attempts failed on two stale assertions (30M versus merged 10M funding
target; passkey bit 2 incorrectly used as an unavailable verifier). Updated
those expectations and the isolated framework's unavailable-factor probe to
bit 4; final suites pass. Removed the obsolete static-label-count assertion
because the actual merged cost fix encodes those labels through shared BCS
helpers; ABI checks, secured-operation tests and the real compatible upgrade
exercise the resulting bytes. No failure was ignored.

The retained PR is ready for fresh independent source review and coordinator
acceptance. Push requests normal exact-head/current-base ordinary/security CI
and protected public native setup/allowed/refused/recovery, with real effects
and spending evidence. Those checks are **pending/unproved**, not waived or
passed. No hosted CI was watched/polled and no machine public mutation,
faucet, signing-state decryption, network lease, allowance increase, cache
reset, new public identity, model worker, new issue or merge was performed. Preserve
cached packages/identities and all unknown reservations when scheduling the
protected lane; do not use SF-228's active lease. The coordinator must enforce
fresh independent source/final review and CI/security/testnet merge gates.
Manual wallet/authenticator, Pi, production provider and actual mainnet checks
remain explicitly unrun in #32/#159.
