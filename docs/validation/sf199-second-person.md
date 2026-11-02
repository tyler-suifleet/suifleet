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
  Only `customer::SecondPersonConfiguration` has a new origin. Enrollment seals
  customer authority at 199; new console/daemon reads support 197 and 199. Old
  customer execution and immutable cancellation routes reject the enrolled state.
  Upgrade the daemon/client before enrollment; this evidence did not roll out any
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
