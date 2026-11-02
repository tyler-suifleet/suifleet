# SF-200 mutable-target feedback repair

Codex `gpt-6.1-sol` repaired the blocking source finding against PR #450 head
`2e25447030cac3e64efa314365c0b550f178a981`. No finer runtime model build ID is exposed.
Functional repair commit: `0ac2e4780635f495d268e527f2785388690244cb`.
The owner branch was preserved without merging or rebasing advancing main.

## Binding audit and repair

Customer already used an object ID. Fleet, global DeviceRegistry, FleetBudget,
GasAccount, FleetStorage, DeploymentRecord and EnrollmentPolicy references now
also bind BCS object IDs. Counters, balances, device receipts, enrollment consumption
and activity in unrelated tenants cannot change those canonical arguments.
Operation names, amounts, recipients, device IDs, original source-witness types,
owned consent/plan values and all other explicit operation inputs stay bound.

Enrollment edits retain their administrator security revision. Deployment references
retain control and target snapshots, but replace the full rollout state with its
administrator decision revision: device outcome/health reports cannot invalidate
an intent, while advance/control decisions still do. DeviceGroup full BCS plus
desired-state revision, AccessPolicy full BCS, catalog full BCS and immutable
release values remain intentional bindings. Those contents change through
administrator/publisher operations, rather than device or cross-tenant activity.
Current proposer/admin, organization generation, custody, registry, membership,
version and underlying operation preconditions remain enforced at execution.

The production console still queues only security settings edits, whose Customer
ID encoding was already repaired. It now exposes/reuses `targetIdentity` for BCS
ID encoding; queue hash/stale-generation/tamper tests still pass. Storage fixtures
use the revised target encoding. OPERATIONS and the design specify the exact new
contract. Older full-mutable-object or full-rollout intents require cancellation
and requeue; no object layouts, public signatures or type origins changed.
The redundant pause attestation expression identified by review is simplified.

## Verification

Exact commands below run inside the pinned container unless stated otherwise.

Pinned `suifleet-sf164:protocol140` image, compiler 1.81.1 and transaction CLI
1.83.0. Move and isolated-chain containers have `--network none`, cached Move
dependencies and an empty temporary compiler keystore. No public wallet mutation
or hardware operation was performed.

- `sui move test --path contracts`: PASS 366/366.
- `sui move test --path contracts/fleet_storage`: PASS 82/82.
- `sui move test --path contracts/fleet_data --build-env testnet`: PASS 60/60.
- `make check-interfaces INSIDE_DEV_CONTAINER=1`: PASS compiled core/storage/data
  ABI, generated drift, ten negative ABI cases, wire vectors and client regressions.
- `node scripts/test_security_delay.cjs`: PASS canonical ID/hash, queue/stale/tamper,
  bounds and real production-console rendering regressions.
- `node scripts/test_security_policy.cjs`: PASS policy and failure regressions.
- `cd dapp && pnpm typecheck && NEXT_TELEMETRY_DISABLED=1 pnpm build`: PASS.
- `git diff --check`: PASS.

New Move regressions execute actual operations: queue revocation, register a device
as an unrelated sender against the global registry, then revoke successfully after
maturity; queue WAL withdrawal, register a managed blob as the device, then withdraw
successfully after maturity. Changing the authorized amount still aborts with
`security_policy::EPolicy`. A rollout test proves device outcomes leave its security
snapshot unchanged and an administrator control decision changes it. Existing
policy-generation, cancellation, timing, old-authority and custody negatives pass.

Initial runner attempts failed before package writes: wrong compiler-cache mount,
missing gRPC helper, and missing worktree Git metadata mount. The existing build
output also required the same container UID as its previous frontend build; rerun
passed. An offline helper build with the wrong Cargo cache path failed dependency
resolution; `cargo build --locked --offline --example chain_facts` with the correct
cache then passed in 34.48 seconds. These setup failures are not application or
chain-test passes.

Fresh independent source review, exact-head/current-base CI, automatic public-testnet
validation and the eventual combined candidate publication-size gate remain the
coordinator's next steps. Hosted CI is pending and is not watched or polled by this
worker. No approval, merge or final acceptance is claimed. Manual/real-wallet,
Pi/hardware, production-provider and mainnet evidence remains unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159);
production hosting remains in [SF-042/#62](https://github.com/tyler-suifleet/suifleet/issues/62).

## Isolated upgrade effects

`python3 scripts/test_security_policy_localnet.py --suite live_security_delay
--base /base/contracts --evidence docs/validation/sf200-feedback-targets-localnet.json`
PASS: existing v197 organization, compatible upgrade, old-package/timestamp attacks,
cancellation, early refusal, mature recovery and restored all-off administration.
Disposable chain `d6129de8`; 16 effects-checked transactions.
Conservative allowance charged 1600117996 MIST of 3000000000 MIST;
signed net gas 1586309496 MIST.
The [receipt](sf200-feedback-targets-localnet.json) records exact digests, verified
unsigned package hashes, type origins, effects and fees. Successful compatible
upgrade proves this source is below the package size limit; the eventual combined
candidate must still pass its own real publication. The receipt's git head is the
precommit parent because the test ran before committing; the fixture source hash
covers the final functional changes, with no functional edits after compilation.
