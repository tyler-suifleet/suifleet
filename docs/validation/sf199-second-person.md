# SF-199 second-person implementation evidence

Codex, configured running model **gpt-6.1-sol** (no finer build/version exposed),
session `dispatch-a4e3e2556cf34a6aa0d970cc1d14567b`, branch
`work/suifleet/sf-199-a46e71f0`. Implements [SF-199/#398](https://github.com/tyler-suifleet/suifleet/issues/398)
on merged SF-196. This records software/test acceptance, not production rollout.

## Behavior and compatibility

- Optional factor bit 4, off by default. Enrollment itself requires an expiring,
  disclosed, generation-bound action approved by another current administrator.
- Current administrator credentials approve/reject; the proposer cannot approve.
  Execution is proposer-only, binds exact canonical arguments, enforces Clock
  expiry/current generation, consumes the action and clears the scoped grant.
- Current-state pending index, bounded to 64 requests, supports dashboard discovery
  without retained events. Arguments are immutable public bytes, at most 16 KiB.
- Existing Customer and Action layouts and introduction origins are preserved.
  Only `customer::SecondPersonConfiguration` has a new origin. The first disclosed
  proposal seals customer authority at 199 before enrollment approval; new console/daemon reads support 197 and 199. Old
  customer execution and immutable cancellation routes reject the sealed state, including the pending enrollment window.
  Upgrade the daemon/client before the first request; this evidence did not roll out any
  existing application deployment.
- Settings and dashboard implement enrollment and policy-change proposals and
  execution, plus independent approval/rejection/cancellation of disclosed actions.
  Other secured operations use their exact existing ABI adapters and Action ID;
  this console does not claim an automatic proposal/execution editor for every
  operation. The dashboard exposes their operation and full argument bytes.
- Verifier code shares the customer module to stay below Sui's 100-KiB package
  limit. Integrating sibling mechanisms must preserve availability bits and verify
  the actual combined package size; standalone size success is not that evidence.

## Verification

Pinned dev image `suifleet-sf164:protocol140`, image ID
`sha256:c6a6d33451dacf606473f9ce7060e66666e29207c939a37627872101f3ffcdd8`.
Release compiler `sui 1.81.1-e04cf9aa915c`; checksum-verified transaction CLI
`sui 1.83.0-16d2dc1e05ed`. Exact identities/checksums are in the receipts.
Final Docker checks used this checkout and private/copied build caches. Other
checkouts' source and target directories were not modified. The unchanged gRPC `chain_facts` reader was
mounted from an existing build; its source matched this checkout byte for byte.

| Command (inside that image unless stated) | Result |
|---|---|
| `sui move test --path contracts` | PASS 361/361 |
| `python3 scripts/check_move_security.py` | PASS pinned compiler, warnings-as-errors, unsuppressed baseline, genuine negative/mutation checks; core 361/361, fleet_storage 79/79, fleet_data 60/60 |
| `python3 scripts/generate_interfaces.py --check` | PASS 15 core modules and generated interfaces |
| `python3 scripts/check_contract_interfaces.py` | PASS 1,279 client references against 17 first-party modules; zero drift |
| `cargo test --locked` in suifleet-edge | PASS 463, zero failures; four existing OCI tests ignored (not passed) |
| `node scripts/test_second_person.cjs` | PASS real SDK/wire builders, current paginated queue, padded type origins, stale/changed snapshots, network/type/customer failures, proposer/approver/expiry/error console rendering |
| `node scripts/test_security_policy.cjs` | PASS original framework/default/unavailable/schema/network regressions |
| `node scripts/test_customer_entitlements.cjs` | PASS including 199 entitlement/read/capacity compatibility |
| `node scripts/test_customer_fleets.cjs` | PASS discovery, revoked authority, multi-fleet isolation and capability/support regressions |
| `node scripts/test_protocol.cjs` | PASS 60 shared recipes, independent BCS vector and SDK encoding |
| `python3 -m unittest discover -s scripts/tests -p test_deployment_manifest.py` | PASS 22 tests |
| `node node_modules/typescript/bin/tsc --noEmit` in dapp | PASS; final production build also runs typechecking |
| `pnpm build` in dapp | PASS static production export |
| `CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/live_wallet_browser.cjs --smoke` | PASS offline wallet bundle boot, no external requests/Node process global; not manual extension-wallet acceptance |
| `make test-ota` with `INSIDE_DEV_CONTAINER=1`, network none | PASS rebuilt real daemon/bins/examples, quilt signer and suite-runner regressions; all 29/29 OTA suites, zero failures (503.3 seconds) |
| `python3 scripts/test_second_person_localnet.py --base /base/contracts --evidence docs/validation/sf199-localnet.json` in a disposable network-none container | PASS 24 effects-checked transactions, existing-object compatible upgrade, old execution/cancel refusal, production-console queue reads and recovery |

The base contracts were exported with `git archive` from the dispatch's initial
HEAD `02bca2dd8efcc2a0c4b72756a8a38eb725e55d0e`. The fixture's head contract digest
is `bytecode-v1:80c600fbd6a6c600d16882f7d893d4a60e2c76d5663c3b5390163375780c1b84`.
This digest matches the local and public receipts. Receipt `head` fields retain
the actual Git HEAD observed during each run; no receipt is relabeled to the later
documentation commit. The padded-tag client fix exercised during acceptance is
committed as `8ecaf255`.

Initial local attempts exposed missing reader/Git mounts, a shared cache ownership
problem (resolved using a private cache), a local faucet startup race, an
insufficient customer-creation gas reservation, an insufficient child *reservation*
(not a waived failure), and package-size overflow. Actual isolated-chain runs
rejected the oversized variants. Compact metadata/shared verifier code resolved
that limit; the final successful upgrade is real, not a build-only assertion.

## Public testnet effects and bounded recovery

Only the dedicated agent `0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965`
and a bounded disposable approver were used, on verified chain **4c78adac**,
protocol 140. Machine mutations held/asserted/renewed `network:suifleet-testnet`;
the lease was released promptly. Coordination:
[planned run](https://github.com/tyler-suifleet/suifleet/issues/11#issuecomment-6565803939),
[actual results/release](https://github.com/tyler-suifleet/suifleet/issues/11#issuecomment-6565912428).

The first 1.8-SUI preflight stopped before any SuiFleet transaction because the
balance after its one faucet attempt was only 1,471,239,134 MIST. The original
helper did not persist that failed preflight's faucet response; its exact outcome
is **not retained**, so neither faucet failure nor a particular credit is claimed.
No second faucet request was made. The harness now persists preflight failures.

A smaller standalone package fixture fit a **1,050,000,000-MIST** ceiling. Its first
six successful transactions exposed a real console bug: SDK `normalizeTypeTag`
preserves padded addresses inside vectors. The strict comparison rejected the
node's fully padded `vector<0x…02::object::ID>`. The client now normalizes those
addresses; its regression uses the actual fully padded tag. This was repaired,
not suppressed or treated as acceptance success.

The [initial receipt](sf199-testnet-initial.json) preserves that failed acceptance
and six actual setup digests. The exact package/fixture was reused without
republishing. After verifying an **all-off** current policy, the unfinished
request was cancelled, then the [successful receipt](sf199-testnet.json) records
19 further transactions, live production-console queue checks and an empty queue
at the end. Its strict 200,000,000-MIST ceiling is smaller than the first run's
remaining 204,275,472 MIST; neither faucet credit, sweep nor refund replenishes
that allowance. The aggregate across both receipts is:

- **25** effects-checked transactions; **908,948,972 MIST** conservatively charged
  allowance, within **1,050,000,000 MIST**.
- **843,256,236 MIST** actual signed net gas across independently re-read effects.
  Every digest records computation/storage/rebate and signed `net_gas_mist`.
  The ledger's nonnegative `gas_mist` intentionally does not recycle rebates.
- Each attempt charged its 20,000,000-MIST approver funding conservatively. The
  recovered gas was swept; the negative second-attempt operator balance delta is
  returned prior funding/refunds, not a negative aggregate test cost.

Selected actual digests:

| Behavior | Digest |
|---|---|
| Publish standalone fixture | `8YrxQVssqYZNKCdfMx4MDuNCJVugMFZgVzthJxNfVhKZ` |
| Independent enrollment and policy enablement | `GuVw4VZiWAm7ciXZEkKYFyHZJivvyHDqKT5oAqno9Rta` |
| Self-approval refused in effects | `APxkeJs1M6vNTwbMVXrFNBKgSX65us49Xcy9nQkNUqJ2` |
| Exact approved action executed | `CsqcRA5cVufBYRn2jifHAMQPLpZoRwZCTQ8qeG6gwYxM` |
| Approved weakening/recovery | `CKZxSjwJ2MoezWZw2YG6ZheZLBLAT4xMxAK9mUF98zmn` |

Public testnet here proves the **standalone mechanism**, not a public existing-
object package upgrade. The full old/new-package and existing-object upgrade
receipt is the separate isolated protocol-140 run. Normal exact-head/current-base
CI, security and automatic testnet gates remain required by the coordinator.
No hosted CI was watched, polled or represented as passed.

Manual extension/Slush/IdP/hardware-key, physical Pi, production provider/hosting
and actual mainnet acceptance remain unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32),
[SF-042/#62](https://github.com/tyler-suifleet/suifleet/issues/62) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159). No production
credentials, original application objects, mainnet or physical programming used.

## PR #453 feedback repair, 2026-11-01

The independent review correctly identified an uncovered pre-enrollment window:
old-package cancellation could delete the first disclosed Action while authority
was still 197, orphaning its pending-index entry. The earlier receipt exercised
old routes only after enrollment and did not prove this window safe.

`propose_approval` now atomically seals authority 199 before creating the first
Action. It validates the first request's supplied customer snapshot and rewrites
only that snapshot to the post-seal bytes used at execution. Cancellation keeps
the seal; requirements remain all-off until explicitly configured. Setup explains
the permanent protocol transition. Existing layouts, ABI and origins are unchanged.
The queue now reports each unreadable/deleted/malformed request individually,
without hiding valid requests or offering execution controls for unreadable data.
Index/network failures still surface as queue errors. The live console checker
rejects partial results rather than treating an unreadable request as acceptance.

Fresh repair verification (same pinned image, user 1001, network none):

- `sui move test --path contracts`: **363/363 passed**, including first-request
  sealing, canonical disclosed snapshot, cancellation preserving the seal and
  all-off policy, and wrong first snapshot rejection.
- `python3 scripts/check_move_security.py`: passed compiler/warnings/security
  baseline, negative fixtures and bypass mutation regressions.
- `node scripts/test_second_person.cjs`: passed, including a deleted/unreadable
  index member alongside a valid request and unavailable-row rendering.
- `node scripts/test_security_policy.cjs`: passed.
- `node dapp/node_modules/typescript/bin/tsc --noEmit --project dapp/tsconfig.json`:
  passed; `pnpm build` in dapp passed static export after the setup text change.
- `python3 scripts/generate_interfaces.py --check`: passed 15 modules;
  `python3 scripts/check_contract_interfaces.py`: 1,279 references, 17 modules,
  zero drift. No generated ABI changes required.
- `python3 scripts/test_second_person_localnet.py --base /base/contracts
  --evidence docs/validation/sf199-feedback-localnet.json`: passed **26**
  effects-checked transactions, including old-package cancellation and secured
  execution refusals **before** independent enrollment approval, followed by
  readable queue, enrollment, exact execution, rejection and recovery. See the
  [fresh isolated receipt](sf199-feedback-localnet.json). This is isolated-chain
  evidence, not a public-network rerun; historical public receipts above retain
  their original source digest.

Initial repair tool invocations failed on host missing `sui`, container cache
ownership/missing Git mount, and unsupported Move indexed-assignment syntax;
these were corrected before the successful checks. Only disposable local wallets
were created; no public wallet mutation or production/physical operation occurred.
Hosted CI/automatic testnet and fresh independent review remain coordinator gates;
this worker does not watch CI or claim approval. Deferred acceptance remains in
#32/#159/#62 as above.

## Stopped-worker recovery, 2026-11-02

Codex running model `gpt-6.1-sol` (no finer build version exposed) recovered
its retained SF-199 claim and PR #453. The initially clean source branch now
includes accepted main `cf65ea175435d1b6e111b988d51d9654a63c9db2` through merge
`249c1f5ae49857df64b9bb806869d35be8724ec0`. This changes coordinator infrastructure
only; the application/Move/interfaces and their prior evidence are unchanged.
No unmerged sibling was imported. SF-221 PR #465 was OPEN at head
`b912f262149d1620345b8edac47de60821dd6093`, with its retained owner in Review;
its cost/source repair is not accepted main and was not copied here.

Read-only artifact recovery used the existing `producer_check` and
`download_fixture` path for main-only run **41392636950**, encrypted artifact
**13118319842**. Producer provenance and single-file/private-mode transport
passed; ciphertext SHA-256 was
`874cb79a4144443fb23ecba86fb52f67ac2fbb950a186d70477739905a76270b`.
The temporary ciphertext was removed after inspection. The protected CI key is
not available locally: decryption and actual on-chain fixture authentication
were **not** performed or claimed. No device identity was read, replaced or
rotated, and no uncertainty marker was changed.

Offline replay consumed public effects artifact **13119410153** from the same
run through the existing Budget class: charged allowance **1,575,909,482 MIST**,
remaining **24,090,518**, unchanged limit **1,600,000,000**. The recorded prepared
fixture pair is `bytecode-v1:043ed77b248edac210044b9d51198e6b117641fbed4ad9a3e985bf2508ca9411..bytecode-v1:068e4d1da36e833c64beb84a9e46357f833de837e6fdf252bf23c8f4a1777eb4@1.81.1-e04cf9aa915c`.
The recorded core phase requires **280,000,000** in full reservations. Replaying
its 200M deployment reservation raised `Spent` before the client was invoked,
without changing used allowance. Separately, effects artifact **13120408740**
from run **41397980111** reproduced its earlier 30M device-funding refusal:
**1,577,001,082** charged, **22,998,918** remaining. These are recorded artifact
replays, not new chain effects or fresh/cached-chain acceptance. No new cost
harness or synthetic successful acceptance was introduced.

Current integration verification:

- Pinned compiler `sui move test --path contracts`: **363/363 passed**.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  **175 tests, OK, one existing skip**.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet_reconcile.py'`:
  **44 passed**, including retained bytes, authenticated identity/provenance,
  restart adoption and uncertainty preservation paths.
- `node scripts/test_second_person.cjs`, `node scripts/test_dashboard_stats.cjs`,
  `node scripts/test_security_policy.cjs`: **passed**.
- Pinned compiler on PATH, `python3 scripts/generate_interfaces.py --check`:
  **15 modules match**. Initial host attempt failed because `sui` was absent
  from PATH; the pinned-compiler rerun passed.
- `cd dapp && ./node_modules/.bin/tsc --noEmit`: **passed**.
- `git diff --check`: **passed**. No new production build, isolated-chain or
  public-testnet acceptance is claimed for this integration-only recovery.

**Blocked dependency:** SF-221 / [PR #465](https://github.com/tyler-suifleet/suifleet/pull/465)
([issue #463](https://github.com/tyler-suifleet/suifleet/issues/463)) owns the
unchanged cost failure. Resume this retained claim once that prerequisite is
verified on merged main, integrate only its accepted interface/source, repair
any resulting SF-199 conflicts, then hand the actual candidate to the sole
coordinator for required CI/public-testnet acceptance and independent review.
Do not repeat fresh publication of identical packages, raise limits, reset
reservations or treat the artifact replay as passing acceptance. SF-199 stays
blocked, not Review-ready. No hosted CI polling or chain writes occurred.
Manual wallet/Pi/mainnet/provider/hosting checks remain unrun in #32/#159/#62.


## Authorized source recovery after cancelled integration wait, 2026-11-02

This supersedes the preceding SF-221 dependency wait. The user cancelled that
integration-only wait: SF-221 is a shared acceptance-cost repair, not an SF-199
API/development prerequisite, and is **not** asserted merged or complete.
Codex running `gpt-6.1-sol` (no finer build version exposed) resumed the original
claim and PR #453. The initial worktree was clean. Accepted main
`3fbf160e25d010ee88f976feb90b2cbbd3f04579` merged cleanly in `b7109bcc`;
only coordinator infrastructure changed, with no application conflict or guessed
unmerged interface. No source defect requiring another application patch emerged.

Fresh checks on that integrated tree:

- Pinned `sui move test --path contracts`: **363/363 passed**.
- `python3 scripts/generate_interfaces.py --check`: **15 modules match**;
  `python3 scripts/check_contract_interfaces.py`: **1,279 references, 17 modules,
  zero drift**.
- `node scripts/test_second_person.cjs`, `test_security_policy.cjs`, and
  `test_dashboard_stats.cjs`: **passed**; frontend `tsc --noEmit`: **passed**.
- `pnpm build` inside the pinned image: **passed**, static production export.
- `python3 -m unittest discover -s scripts/tests -p 'test_testnet*.py'`:
  **175 tests, OK, one existing skip** (offline fixtures, no public transactions).
- Accepted-main coordinator `test_shared_acceptance_repair.py`: **13 passed**;
  `test_batch_integration.py`: **40 passed**.
- Network-none pinned-image `python3 scripts/test_second_person_localnet.py
  --base /base/contracts --evidence docs/validation/sf199-recovery-localnet.json`:
  **passed 26 effects-checked transactions**, existing-object compatible upgrade,
  old/current bypass refusals, distinct approval, exact execution, rejection,
  recovery and live console queue reads. [Receipt](sf199-recovery-localnet.json).
  The receipt retains HEAD observed at run start (`17cd0e75`), before the merge
  commit was created; the tested application/contracts tree equals the integrated
  tree. No receipt was relabeled. Local allowance charged 1,648,869,799 MIST;
  this disposable-chain setup does not spend public funds or change CI limits.

Initial disposable invocations failed on missing warm Move cache; rerunning with
this claim's existing cache passed. The host lacks pnpm; the production build
passed in the pinned image. These setup failures were not counted as passes.

Local/source implementation is ready for **independent SOURCE review** under the
explicit recovery direction. This is not final acceptance-ready or merge approval.
Exact-head/current-base hosted CI, protected automatic public-testnet validation
and fresh independent final acceptance remain pending mandatory merge gates.
No hosted CI was watched or polled, no unchanged failing public fixture repeated,
no wallets created on public networks and no spending controls changed. Historical
standalone public evidence above remains historical, not current-base success.
Manual wallet/Pi/mainnet/provider/hosting checks remain unrun in #32/#159/#62.

## Pinned batch conflict repair, 2026-11-02

Resolved the actual conflict against pinned accepted main
`df5d6d8005801034a75d4ce4180dcf713d82efd8`, which includes SF-201 native
multisig. Both mechanism implementations, settings panels, generated calls and
operator guides are preserved. Availability is the union (5); the second-person
console bit still requires its introduction origin. Policy tests now cover
0/1/4/5 and reject unsupported bits. This is a conflict repair, not a claim that
the original reviewed source failed its standalone checks.

Fresh combined-tree checks:

- Pinned `sui move test --path contracts`: **373/373 passed**.
- `python3 scripts/generate_interfaces.py`, then `--check`: **16 modules match**.
- `python3 scripts/check_contract_interfaces.py`: **1,292 references against 18
  modules, zero drift**.
- `node scripts/test_second_person.cjs`, `test_security_policy.cjs`,
  `test_multisig_admin.cjs`, `test_dashboard_stats.cjs`: **passed**.
- `cd dapp && ./node_modules/.bin/tsc --noEmit`: **passed**.
- Pinned image `node scripts/test_multisig_admin_browser.cjs`: **passed**, actual
  Chromium with fixture wallet/transport; `cd dapp && pnpm build`: **passed**.
- Network-none `python3 scripts/test_second_person_localnet.py --base
  /base/contracts --evidence docs/validation/sf199-combined-localnet.json`:
  **FAILED** at compatible upgrade: `MovePackageTooBig { object_size: 104427,
  max_object_size: 102400 }`. [Failure receipt](sf199-combined-localnet.json)
  records three successful setup transactions and the failed upgrade effects;
  no subsequent second-person behavior was tested in this run. Its HEAD is the
  pre-merge run-start revision; it is not relabeled as final-head acceptance.

Setup failures: first container used the wrong uid and could not write; a host
interface check used a container wrapper that did not share its temporary summary
path; both passed on corrected invocations. The first isolated run referenced an
unmounted `/workspace` chain-facts path and stopped before publication; correcting
the path exposed the real package-size failure above. A unittest discovery command
for `test_generated_interfaces.py` found zero tests and is not counted as testing.

**Blocked:** the combined package is 2,027 bytes over the protocol limit. A
compatible source-size reduction or reviewed package-boundary design is required,
then rerun isolated-chain upgrade/behavior checks and the coordinator's exact
candidate CI/public-testnet gates. No spending limit or protocol cap was changed.
The merge-conflict repair alone does not complete acceptance. Existing PR #453 /
SF-199 remains the owner of this integration finding; no new issue was created.
No public-wallet mutation, hardware operation, hosted-CI polling or main merge
occurred. Manual wallet/Pi/mainnet/provider/hosting remain unrun in #32/#159/#62.
