# Move security audit and gates (SF-050)

Status: **blocked, not formally verified or Review-ready**. Audit performed by
Codex, running model **gpt-6.1-sol**, session `codex-move-security-20261015`,
on 2026-10-15. Baseline: `54a68504afce1b1dc73d57eb708e4607cd74061e`;
branch `work/suifleet/sf-050-533dfe05`. Scope is the seven modules under
`contracts/sources`, their tests, and existing upgrade/effects harnesses.
This is a source audit plus executable checks, not a complete security proof.
Remaining work is retained in [SF-050/#82](https://github.com/tyler-suifleet/suifleet/issues/82).

## Recovery on 2026-10-22: software proof blocker retained

Codex running **gpt-6.1-sol** resumed the same reservation/session and integrated
main `7e899f32270337aee6ed113a499fcf1b5201d63e`. This is a software blocker,
not deferred Pi/mainnet/provider acceptance. PR #83 remains draft; issue #82
remains open. No successful formal verification is claimed.

The network-none pinned gate passed on the merged source: **168/168 Move
tests**, strict compiler/extra linters, exact unsuppressed diagnostic baseline,
both lint negatives, linear-capability negative and both real authority-removal
mutations. The initial merged-source run correctly failed the baseline check
on 15 new diagnostics. Those were reviewed individually: 13 existing immutable
`TxContext` signatures, the intentional authenticated-sender gas transfer in
`fleet_budget::draw_gas`, and the compatibility `fleet_budget::reserved` getter
(which returns zero because reserves live in `GasAccount`). The baseline records
the reasons. No new suppression or executable contract change was introduced. Both release
builds produced identical arrays for all **11 modules**, compared against main
`7e899f3` with the pinned compiler/framework; serialized-array SHA-256
`1e5d1b39c20e5e19d224aa12eba924f8db550919a86ac0546c595bfc723f4e19`.
This establishes unchanged release bytecode, not formal security.

The original seven-module source audit and earlier eight-module evidence below
are historical. The compiler/test gate now covers the eleven modules of the
root package. Additional boundary review for this recovery:

- `deployment_manager::{advance_rollout,control_rollout,delete_rollout}` checks
  the creator. Health and online lists are operator attestations; they are not
  chain verification of off-chain telemetry. The rollout state machine and all
  new adversarial tests pass, but have no formal proof here.
- `fleet_handover::{confirm,step,finish}` checks recipient confirmation, source
  execution, inventory/kind, expiration and completion. Owned cap transfer uses
  transaction input authorization; group/deployment transfers and role/policy
  operations retain their own authority checks. Cancellation does not undo an
  already completed transfer. No exhaustive PTB-composition proof is claimed.
- `fleet_budget::{draw_gas,assert_may_draw}` binds the budget, registry, current
  gas account, active signer and enrolled owner cap, then enforces the reserve,
  period, amount and rate limits. `seal_policy::grant_group` requires both the
  policy admin cap and the current group admin sender. These are inspected
  source boundaries and regression evidence, not formal verification.
- The separate `contracts/fleet_storage` package is **not** covered by this
  root-package compiler/lint runner or the original audit. Complete its analysis
  and gate coverage before claiming an audit of all current contracts complete.

### Upstream compatibility investigation (source evidence, not a proof run)

Fetched the full upstream prover history on this date. `main` still resolves to
`949d14419515038d341d440e9c01c0737adf2ff0`; the releases API returns no releases.
The unchanged failing evaluation was deliberately **not rerun**. A different
framework/model combination must be established first.

1. At the pinned release framework, `sui::object::new_uid_from_hash(bytes)` calls
   `record_new_uid(bytes)` (`object.move:270-281`). The prover's commit
   `c710bd57d13cec3e9c81776ac11c7ff006e9a8a4` changed both the object spec and
   the Rust model lookup to `record_new_uid_from_hash(parent, bytes)`. Merely
   renaming the spec leaves the Rust intrinsic lookup incorrect.
2. Checked predecessor `82b1f464d4f1b2c414c5cd4d9e08560d34295222`: its object
   spec and Rust lookup still target `record_new_uid`, and its accumulator spec
   does not require the newer reservation native. However, its prover vector
   package still imports missing `std::integer::Integer`; its compiler fork is
   `512c3ff9a14608d281f4b1095508f59cf7ad6cce`, not the release framework. This
   candidate is **not a verified compatible toolchain**, and was not built or
   run as a passing verifier.
3. Examined `integer.move` at the current pinned compiler fork. It is a spec-only
   native arithmetic type, but its extension methods depend on `real`, `q32`,
   `q64`, `q128`, `q_wad` and `q_ray`, which are absent from release stdlib.
   Copying just this file is not a complete compatibility port. Replacing all
   release framework bodies with the fork would change the verification subject.
4. Current upstream `dynamic_field_analysis.rs:790-830` explicitly rejects
   datatype well-foundedness cycles; its
   `dynamic_field/well_foundedness_cycle.fail.move` fixture exercises this.
   This is a source-confirmed model limitation, **not a newly reproduced failure
   of SuiFleet custody**: SuiFleet model construction has not yet reached that
   phase. Establish whether typed parent resolution handles the real custody
   functions after fixing the earlier errors; do not assume it does.

Reproduce the source comparison without executing a solver (in a checkout of
`https://github.com/asymptotic-code/sui-prover.git`):

```bash
git show c710bd57d1 -- packages/sui-specs/sources/sui-framework/object.move crates/move-model/src/model.rs
git show 82b1f464d4:packages/prover/sources/vector.move
git show 82b1f464d4:packages/sui-specs/sources/sui-framework/object.move
git show 82b1f464d4:packages/sui-specs/sources/sui-framework/funds_accumulator.move
git show 949d144195:crates/move-stackless-bytecode/src/dynamic_field_analysis.rs
```

**Minimal next step:** produce and independently review a release-compatible
model port: spec-only arithmetic support with its complete dependencies, the
release `record_new_uid` signature **and Rust intrinsic mapping**, and accumulator
models matching release natives. Preserve and compare all executable release
framework bodies. Run `evaluate_move_prover.py` against that pinned port with
real Boogie/Z3; require model/type checking and non-vacuous successful proofs,
then deliberate solver-rejected invariant mutations. Resolve any actual custody
model limitation rather than erasing custody or assuming its invariant. Only
then wire the verified subset into normal CI. No local unsupported-native stub,
`ignore_abort`, trusted replacement body or vacuous precondition was added.

Remaining source/acceptance work stays in #82: Low L-01 legacy cross-registry
accounting (old packages remain callable, so an in-place new-function assertion
alone cannot eliminate the old-entry attack); the historical receipt harness's
manifest tail still requires a truthful historical/current client test strategy;
and complete critical receipt/capability/revocation/enrollment/binding proof
coverage. Prior 47-check identity and 33-check partial historical receipt results
below were **not rerun or relabeled** as current-head acceptance. Physical,
mainnet and final provider/wallet acceptance remain separately tracked in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159); this does not
defer software proof, source-correctness, compatibility or CI gates.

## Historical recovery validation on 2026-10-15

On 2026-10-15 the reserved SF-050 worker was safely resumed after verifying
its prior service/PID had stopped. Main at
`795de747e066ee97af0e9e0e53deda3c6c71e90c` was integrated (the exact baseline
is also the merge commit's second parent). The audit now includes the eighth
module, `desired_state`, and the deployment-control APIs. Historical evidence
below remains historical; this section records the new checks.

- Corrected the hosted CI container/cache identity mismatch by building with
  the runner UID/GID, following ordinary CI. No root compiler execution.
- Pinned standalone negative controls to the release `Move.toml`/`Move.lock`.
  A fresh manifest had selected a newer implicit framework; a compiler-version
  check alone did not pin those dependencies.
- Reviewed three new baseline diagnostics: `deployment_manager::set_control`
  and `desired_state::{begin,commit}` use already-published `&TxContext` public
  signatures. Changing those signatures would break compatibility. Existing
  module-level allowances are independently stripped by the baseline check.
- The complete gate passed with `--network none`: strict compiler/lints,
  exact unsuppressed baseline, both lint negative controls, linear-capability
  negative, **82/82 Move tests**, and both authority-check removal mutations.
- All **eight** release module byte arrays equal current main, serialized-array
  SHA-256 `5d8bb6d05e67fb5b9706cc374ea5e2661731133099a6f08b634760568ca70b99`.
  No source function body or public signature was changed by the audit repair.
- `actionlint 1.7.7` and `git diff --check` passed. Current-head hosted workflow
  results are recorded in PR #83; local success is not a hosted-CI claim.

`desired_state::begin/commit` checks group admin, group identity and stale
revision; `set_component` requires creator, target-group membership and an
authenticated recipe. The non-droppable `RevisionPlan` cannot escape its PTB.
`deployment_manager::set_control` checks creator, state/schedule constraints
and irreversible cancellation; removal before its cancellation check is atomic
on abort. These are source-review and regression-test observations, not proofs.
None of the new controls disables old package entry points; existing mixed-
version trust rules remain necessary.

The pinned prover upstream HEAD is still `949d14419515038d341d440e9c01c0737adf2ff0`.
A release-compatible model port remains required: preserve release executable
framework bodies while supplying reviewed spec-only integer support and models
matching the release native signatures. Substituting the fork's moving `next`
framework or deleting unsupported checks would not prove this release. That
port and successful positive/negative proofs are still unmet SF-050 acceptance.

## Authority map and findings

Severity means potential impact within the current client trust model. No
Critical/High defect was established in the current implementation by this
audit; absence of a finding is not proof of absence. Historical vulnerabilities
and current limitations are distinguished below.

| File/functions | Boundary and concrete assessment | Severity/status |
|---|---|---|
| `deployment_manager.move`: `make_deployment`, `receipts_mut`, `record_status`, `migrate`, `receipt_status*` | New records omit `StatusesKey`. Authoritative `ReceiptsKey` has its own introduction origin. Version 2 writers reject future generations and discard prior-generation statuses. Readers reject prior generations; legacy events are notifications, not evidence. Old packages remain callable. A sweep does not prevent old code creating fresh version-1 records, so every reader must check generation. | Historical High receipt forgery mitigated; adversarial unit/effects tests retained. No proof of all future upgrades. |
| `device_cap.move`: `issue`, `assert_signer`, `assert_active` | Public issuance does not grant impersonation: the cap's private UID holds its registered signer binding. Unbound legacy caps fail. Active reporting additionally needs live custody, unrevoked identity and current signer. Transferring a cap does not change its signer. | Checked by current tests, including operator impersonation, revocation and rotation failures. |
| `device_registry.move`: `activate_device`, `borrow_device`, `return_device`, `rotate_device_key`, `revoke_device`, `reinstate_device`, `deregister_activated_device` | `DeviceOwnerCap` is bearer authority. Custody plus a linear, non-droppable `DeviceLoan` prevents passing a Device by value to old deregistration code and committing without returning it. Return checks ID and signer. Rotation preserves revocation. Deregistration leaves a tombstone. The owner-cap holder can issue a new bound device cap through a temporary loan. | Current unit coverage; custody representation and cross-version linkage remain outside formal coverage. Transferring/wrapping a bearer cap delegates its authority. |
| `device_registry.move`: `register_device`, `deregister_device` | **Finding L-01:** the registry counter is incremented/decremented through the supplied registry, but the legacy Device has no originating-registry binding. A holder of an unactivated Device can supply another registry with a nonzero counter and corrupt its accounting. With zero count the subtraction aborts atomically. This does not forge active custody or receipts. Do not use this count as an authorization/quota source. | **Low, open**; source finding, no fresh exploit transaction claimed. Repair must retain legacy layouts and test old/new registry behavior. Reported in #82; no new issue created. |
| `enrollment.move`: `create_policy`, `add_claims`, `revoke_claim`, `set_limits`, `close`, `enroll` | Shared policy mutations check its operator. Enrollment binds registry, open flag, Clock expiry and quota; consumes a claim identified by gas sponsor, requires sponsorship and a distinct device sender, then atomically activates custody and transfers caps. Runtime signature validation, not a Move address comparison alone, establishes sponsor/device key possession. Aborts roll back claim/quota changes. | Existing positive/negative tests retained. Operator can deliberately re-add an already consumed claim address; “single use” means consumed until operator reauthorizes it. Provisioning must not recycle claim keys unintentionally. |
| `roles.move`: `allow_group`, `grant`, `revoke`, `is_active`, `create_deployment_as`; `deployment_manager.move`: `create_delegated` | Builder has no abilities and must be completed. Groups are checked against the immutable operator/admin. Shared registry activation is required on every delegated deployment; revocation checks recorded operator. Every target must be in scope; empty targets fail. Attributed creation is package-private. Delegate cannot use this API for direct devices or creator-only deletion. | Revocation/scope/unauthorized revoker/delete tests retained. Role is transferable bearer authority, not bound to the address logged in `RoleGranted`. A holder may further delegate via transfer/wrapping; do not interpret that event as a permanent signer ACL. |
| `device_group.move`: `add_device`, `remove_device`, `delete_group`; `deployment_manager.move`: `report_verified_status_for_group` | Shared group writes require admin. Group reporting requires targeted group ID, current membership and group admin equal to deployment creator. Enrollment/device custody does not make a group admin the device operator. Public `custodied_device` permits reading another operator's Device, so group membership alone is not operator consent. | Current targeting checks retained. Device operator authorization before apply remains a client/daemon boundary. |
| `deployment_manager.move`: `create_deployment`, `create_deployment_verified`, `report_verified_status`, `delete_deployment` | Legacy creation lacks a recipe digest and must remain unusable by secure clients. Verified creation validates a lowercase 64-byte recipe SHA-256 string, not Walrus bytes. Artifact digest strings in this legacy API are not format-validated on chain. Direct creation/reporting validates targeting and live signer, **not creator ownership of the device**; a receipt attests a device report, not independent proof of safe firmware/operator approval. Deletion checks creator. | Intentional client/daemon integrity and authorization boundary, not established by compiler/prover. Unknown/malformed hashes must fail before updater execution. |
| `component_catalog.move`: `publish_version`, `create_deployment`, `delete_deployment`, validators | Shared catalog updates require publisher. Name/version/platform/digest/token and vector consistency checks precede immutable version freezing. Duplicate versions/platform pairs fail. Deployment binding uses the immutable version ID; catalog deletion removes it through creator-checked package APIs. Choosing a version from another publisher is possible and must be an explicit operator trust decision. | Current catalog tests retained. Hash strings authenticate expected bytes only when clients actually verify downloads. No Walrus availability/certification proof. |

Upgrade compatibility depends on the deployment manifest's per-type origins,
the UpgradeCap holder and the actual package version used for each transaction.
The compiler cannot disable old packages, rerun initializers or migrate shared
objects. A future authority upgrade must fence existing writer generations,
migrate existing state and test old-package attacks. UpgradeCap compromise,
malicious future code, chain/runtime/native correctness, arbitrary PTB composition,
off-chain RPC integrity, download verification and updater execution are outside
the proofs attempted here. See [operations](OPERATIONS.md) and
[deployment manifests](DEPLOYMENT_MANIFEST.md).

## Compiler, linter and regression gate

Use the checksum-pinned Dockerfile: Sui `1.81.1-e04cf9aa915c`, archive SHA-256
`d921910fdedf5ae2bc3f08a7236c57819a457ce53a8cc60c85f3653c7831b5d4`.
The runner rejects a different CLI version and uses an explicit empty keystore;
it does not generate or access wallet keys. Source is copied to private temporary
directories. With the framework cache already populated it also runs offline:

```bash
docker run --rm --network none \
  -v "$PWD:/workspace:ro" -w /workspace \
  -v sui_move-cache:/home/dev/.move \
  suifleet-dev:sf004 python3 scripts/check_move_security.py
```

`.github/workflows/move-security.yml` runs this gate on contract, runner,
workflow or Dockerfile changes. It has a read-only token and no secrets. CI may
fetch the locked framework sources into its dedicated cache; no transaction is
submitted. It is separate from SF-048's web/dependency security workflow.

The positive release build uses `--lint --warnings-are-errors`. Existing
immutable `&TxContext` signatures and `public entry` flags get function-level
exceptions, not incompatible API edits. The retained legacy event gets an
unused-field exception; sponsored enrollment intentionally transfers the cap
to its authenticated sender. One inferred `u64` is now explicit. No other
runtime behavior changes.

The runner also strips **all** `allow` annotations from a separate copy and
requires exactly the 46 reviewed diagnostics in
`contracts/security-lint-baseline.json`, keyed by file, enclosing symbol and
diagnostic code (including multiplicity). This expected diagnostic build exits
1; any unexpected diagnostic/status fails the gate. This additional pass matters:
in the tested pinned CLI, adding/removing an individual public-entry exception
did not reliably surface an unannotated public-entry warning elsewhere in the
same annotated package. The unsuppressed pass demonstrably rejects that new
warning in `roles.move`. Baseline changes require review, never automatic capture.

Other genuine negative controls are a separate Sui package rejected by the
public-entry linter, destruction of a struct without `drop` rejected by the
compiler, and actual implementation mutations removing the active-role and
active-identity checks. The last two must reach the targeted Move test and fail
with `Test did not error as expected`; a compilation failure is not accepted.
These are finite regression checks, not exhaustive authorization proofs.

## Formal tool evaluation: reproducible blocker

Primary sources: [Asymptotic Sui Prover repository/README at the evaluated commit](https://github.com/asymptotic-code/sui-prover/tree/949d14419515038d341d440e9c01c0737adf2ff0),
its `crates/sui-prover/src/system_dependencies.rs`, `packages/*/Move.toml`,
`Cargo.lock`, and [official documentation link](https://info.asymptotic.tech/sui-prover)
(returned HTTP 404 during this run). The repository's latest commit was dated
2026-09-25, and its CI contains real positive/negative verifier fixtures.
This is a Sui-specific prover using Boogie/Z3; neither an editor analyzer nor
the generic Aptos prover was substituted.

All evaluated revisions/checksums are in
`contracts/security-specs/toolchain.json`: prover commit
`949d14419515038d341d440e9c01c0737adf2ff0` (binary reports `1.5.3`), its locked
compiler fork `a30b3efcfaa333241c1864581753128eedc5dbf1`, Boogie fork
`b104805d9070f99a0b494b7760f82a274926c171` (`3.5.6.0`), Z3 `4.13.4`, .NET SDK
`8.0.426`, Rust `1.94.0`, and the **release** framework revision
`73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1` from `contracts/Move.lock`.
The Homebrew formula's moving `main`/`next`/`master` references were not used
as version pins. Boogie was built from its pinned source; its transitive NuGet
resolution is not promoted here as a locked production verification toolchain.

Reproduction after cloning/checking out the above exact commits and downloading
the checksum-verified SDK/Z3 archives:

```bash
# Inside the existing pinned dev image; TOOLS is a private temporary tools root.
cd "$TOOLS/sui-prover"
CARGO_BUILD_JOBS=2 cargo build --locked --release -p sui-prover
cd "$TOOLS/boogie"
DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 "$TOOLS/dotnet/dotnet" \
  build Source/Boogie.sln -c Release -m:2
cd /workspace
export DOTNET_ROOT="$TOOLS/dotnet"
export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
export BOOGIE_EXE="$TOOLS/boogie/Source/BoogieDriver/bin/Release/net8.0/BoogieDriver"
export Z3_EXE="$TOOLS/z3/z3-4.13.4-x64-glibc-2.35/bin/z3"
python3 scripts/evaluate_move_prover.py \
  --prover-source "$TOOLS/sui-prover" \
  --framework-packages "$TOOLS/framework" \
  --prover-binary "$TOOLS/sui-prover/target/release/sui-prover"
```

`$TOOLS/framework` must contain `move-stdlib/` and `sui-framework/` copied from
`crates/sui-framework/packages/` at the release revision. The script checks their
combined source hash and the prover revision/lockfile, and stages sibling spec
and implementation packages. It adapts only package metadata: the legacy prover
resolver needs an explicit `sui_edge = 0x0` named address, has no release CLI
environment lockfile, and uses local paths for the pinned release dependencies.
No implementation or framework function body is modified. Prover model packages
are copied from the pinned prover with only moving dependency URLs replaced by
these local paths. All model code remains intact.

**Observed result: exit 1, model-building errors, before solver execution.**
`prover/sources/vector.move` imports missing `std::integer::Integer`.
`sui-specs/sources/sui-framework/funds_accumulator.move` requires absent
`reserve_object_funds_for_withdrawal`; `object.move` requires absent
`record_new_uid_from_hash`. The default prover instead injects its fork's moving
`next` framework, which would not establish correctness of this release.
The evaluated prover also has an upstream negative fixture rejecting dynamic
fields whose value contains UID (`well_foundedness_cycle.fail.move`); that is
an additional modeling concern, **not** a reproduced failure of this project's
custody functions, since model building already stops earlier.

The sibling specs target actual `enrollment::close`, `set_limits` and
`device_registry::is_active_signer`. They state operator abort conditions,
closure/quota stability and signer/revocation/custody conditions without
`ignore_abort`, trusted replacement bodies or false preconditions. The identity
spec explicitly assumes custody has an identity; proving preservation of that
representation invariant is still required. **None of these specs has passed
verification or completed type/model checking.** No solver-positive result or
deliberately failing solver-negative result is claimed. The tools were built,
but no theorem was verified. Compiler mutation tests do not fill that gap.

Formal receipt isolation, capability lifecycle, scoped revocation, enrollment
claim consumption and hash/target invariants remain unmet scope. A compatible,
pinned and reviewed framework/native model (including reachable-state invariants)
must first be supplied; then run real successful proofs and deliberately broken
specifications/implementation mutations with counterexamples. Wire that verified
subset into CI and finish the remaining essential proofs before Review. The
current workflow intentionally advertises compiler/lints/regressions only, not
successful formal verification. SF-050 software/formal acceptance is not deferred by the physical/mainnet/provider acceptance split.

## Current validation

| Command/check | Result |
|---|---|
| `python3 scripts/check_move_security.py` in pinned network-none image | PASS: strict build, exact unsuppressed baseline, both lint negatives, linear-ability negative, 66/66 Move tests, both authority-bypass mutations rejected |
| Baseline/current `sui move build --dump-bytecode-as-base64 --no-tree-shaking` | PASS: all seven module byte arrays identical; serialized array SHA-256 `c186e6ac36e1ae01e13909838848ffdfbea4b7d6052d082939a097f8df3875c9` |
| Prover `cargo build --locked --release -p sui-prover` with two jobs | PASS, 8m57s; built tool only |
| Pinned Boogie build with two jobs | PASS, 0 errors, 195 upstream warnings; reports 3.5.6.0 |
| `python3 scripts/evaluate_move_prover.py ...` | FAIL (exit 1): incompatible models as above; formal acceptance blocked |
| `actionlint 1.7.7 .github/workflows/move-security.yml` | PASS |
| `cargo build --locked --manifest-path suifleet-edge/Cargo.toml --example chain_facts` with two jobs | PASS (prerequisite for the existing upgrade harness) |
| `python3 -m unittest discover -s scripts/tests -p test_upgrade_security_localnet.py -v` | PASS, four CLI parsing/local-only safety regressions |
| `python3 scripts/identity_lifecycle_localnet.py --prior /tools/prior/contracts --new contracts` on disposable network-none 1.81.1 localnet | PASS, 47/47 checks, real upgrade/old-package calls, exact abort sites, live receipt state and current dApp receipt reader |
| `python3 scripts/upgrade_security_localnet.py --v1 /tools/initial/contracts --v2 /tools/receipts/contracts` on separate disposable localnet | FAIL overall: 33 passing receipt/attack/effects checks, then current manifest resolver requires later identity/enrollment/role types absent from the historical package |

For those local runs, `/tools/initial/contracts` was archived from `eb8a43b`,
`/tools/receipts/contracts` from `8728d61`, and `/tools/prior/contracts` from
`81f0bf8^` (`d3acb1e1628ce6ff53949d74616907e8c3ab504b`). Each run used
private temporary `SUI_CONFIG_DIR`, `sui start --with-faucet --force-regenesis`,
an explicit loopback-only client config and a captured disposable bootstrap
`new-address` result. Startup waited for gRPC chain identity and the faucet;
the node was terminated and private keys/logs deleted afterward. No existing
wallet or chain was mounted. The earlier harness cannot currently finish its
frontend-manifest tail against those historical packages; do not call its
33 successful checks a passing end-to-end run. The chain-ID parsing repair
does not weaken the current resolver's required-type check. The current
identity suite independently passed all 47 checks.

Selected local execution effects from the successful current identity run:
revoked old-package write after sweep
`G16npXhvFPBPFAUkPgxhJuxY3Gf9PkR8hHfP4eJ17c1v` executed successfully but
remained untrusted at receipts version 1; the current active-device report
`BNSqkapw8fnEVQnLYYE37CCD2VTjhfx31fKdX2pgiT7U` replaced it with version-2
state containing only the active device. These digests belong to a destroyed
local chain, not public-network acceptance receipts.

The byte comparison is against the audit baseline with identical compiler and
framework inputs. It establishes unchanged release bytecode/layouts/signatures,
not security or public-network acceptance. No public wallet, mainnet, production,
Walrus or physical-device operation is part of this audit. Upgrade-effects and
current-head CI results are recorded in #82/the draft PR; incomplete runs are
reported as failures, never as acceptance.
