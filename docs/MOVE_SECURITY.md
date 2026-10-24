# Move security audit and gates (SF-050)

The implementation supplies source auditing, pinned compiler/linter gates,
conditional formal proofs and real adversarial regressions. It does not prove
all contract or native behavior secure. Current implementation owner: Codex running
**gpt-6.1-sol**, session `dispatch-47662e15ede74d09a4fb894e71b7e7e1`, on
`work/suifleet/sf-050-76b02498`; [SF-050/#82](https://github.com/tyler-suifleet/suifleet/issues/82)
and [PR #251](https://github.com/tyler-suifleet/suifleet/pull/251). The recovered
implementation and earlier evidence remain in superseded [PR #83](https://github.com/tyler-suifleet/suifleet/pull/83).
Current-head CI, automatic testnet and independent review are recorded in the
issue/PR handoff; historical results are not current-head acceptance.
Older failed prototype claims in Git history are superseded by the current
[model and exact coverage](MOVE_NATIVE_MODEL.md).

## Pinned formal environment and proved scope

Asymptotic's Sui-specific prover was evaluated against the actual Sui 1.81.1 /
Move 2024 release framework. Its unmodified framework model failed before the
solver: absent spec-only Integer and mismatched fork-only native functions.
A checksum-pinned release adapter and native backend port now resolve that
compatibility gap, preserving release executable framework/application bodies,
UID cycle checks, public layouts and introduction origins. An Aptos prover,
language server or compiler success was not substituted for security analysis.

`contracts/security-specs/toolchain.json` pins every overlay and tool: prover
`949d14419515038d341d440e9c01c0737adf2ff0` (1.5.3), compiler fork
`a30b3efcfaa333241c1864581753128eedc5dbf1`, release framework
`73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1`, Boogie
`b104805d9070f99a0b494b7760f82a274926c171` (3.5.6.0), Z3 4.13.4,
.NET SDK 8.0.426 and Rust 1.94.0. Cargo and fourteen runtime NuGet dependencies
use locked resolution. Binary archives and overlay/source inputs have checked
digests. The evaluator rejects altered/untracked backend code. Build the binary
from the reviewed patched source; source checks alone cannot authenticate an
arbitrary supplied binary. Primary tool source:
[Asymptotic at the evaluated revision](https://github.com/asymptotic-code/sui-prover/tree/949d14419515038d341d440e9c01c0737adf2ff0).

| Gate | Positive phases / intended solver negatives | Exact property boundary |
|---|---|---|
| `check_move_proofs.py` | 63 / 25 | Arbitrary-state identity signer/revocation/custody read behavior; cap signer; role state/revocation; enrollment operator/claim controls; group/deployment handover; receipt generations and legacy reporting rejection; recipe format and artifact/recipe/blob binding. |
| `check_move_native_proofs.py` | 24 / 3 | Raw typed/untyped occupancy, unknown tags, exact missing/type/duplicate errors, mutation/removal and parent/name framing. |
| `check_move_object_proofs.py` | 21 / 9 | Two-slot DOF, arbitrary input add, multi-child framing, counter/last-created/legacy-field observations, distinct allocations, local ownership production and internal-call/return obligations. |
| `check_move_registry_proofs.py` | 42 / 5 | Actual initializer, registration/deregistration, activation/return/borrow, identity writers and role-field framing preserve the quantified registry invariant. Actual full enrollment consumes the claim and updates quota/count. |

Each positive includes Check, Assume and SpecNoAbortCheck. Negatives must fail
their specified solver phase/diagnostic with the other expected phases passing;
timeouts, parser errors, unsupported projections and empty runs fail the gate.
The retained-owner control intentionally fails both real ownership obligations
(Check and Assume); the specification well-definedness phase still passes.
Compiler/linter negatives and native-VM executions are separate evidence.

The registry invariant says existing private IdentityKey entries are typed, and
an occupied private custody name has a real Device of the matching ID and a typed
identity. It is checked at construction and preserved by the closed writers;
it is not added as a freshness/custody `requires` to make a failing call pass.
The actual initializer is renamed only in the disposable proof copy because
Move prohibits directly invoking `init`; every body statement remains intact.
A wrong-typed initializer and omitted activation identity fail real obligations.
The role-add frame verifies the exact sole registry write in `grant`, checked
against its source statement. This establishes frame preservation, not all
allocation/transfer/abort behavior of `grant`. Public read-only registry APIs,
private key visibility and package-only UID exposure to roles close this writer
scope. Old packages never acquire the later private key constructors.

The ordinary 63/25 gate deliberately runs without the registry type invariant,
retaining unknown, malformed and orphan arbitrary-state cases. `no_opaque` keeps
real mutator bodies where an incomplete modular summary would lose storage
framing. No failed essential registry/enrollment proof is excluded from CI.

These proofs are **conditional on the trusted Sui native/VM contracts and the
explicit cryptographic allocation abstraction** in MOVE_NATIVE_MODEL.md. They
do not verify Rust hashing/collision resistance, package/transaction admission,
gas or native resource limits, Walrus, external services, future malicious
upgrades, arbitrary PTBs or complete business logic. Unsupported reconstruction
and namespace shapes fail closed; no proof claims those shapes supported.

## Reproduction and evidence

Inside the pinned dev image, mount a private tools cache at `/tools`, the checkout
read-only at `/workspace`, and the pinned Move cache. Set DOTNET_ROOT=/tools/dotnet,
DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1,
BOOGIE_EXE=/tools/boogie/Source/BoogieDriver/bin/Release/net8.0/BoogieDriver and
Z3_EXE=/tools/z3/z3-4.13.4-x64-glibc-2.35/bin/z3. Use two Cargo jobs:

```bash
python3 scripts/build_move_prover.py --tools /tools
python3 scripts/check_move_security.py
python3 scripts/check_move_native_vm.py
python3 scripts/check_move_proofs.py --tools /tools
python3 scripts/check_move_native_proofs.py --tools /tools
python3 scripts/check_move_object_proofs.py --tools /tools
python3 scripts/check_move_registry_proofs.py --tools /tools
python3 scripts/test_move_security_localnet.py --help
```

The normal contract CI job runs all four proof gates and release-VM/compiler
controls without wallets/secrets. Exact final commands/results, unchanged release
byte arrays, isolated-chain effects and current-head hosted checks are recorded
in #82/PR #83. Prior network-none local-chain evidence on 2026-10-22 was 35/35
historical receipt/old-package checks and 47/47 current identity checks; the
historical harness genuinely upgrades to current source before resolving the
manifest, preserving original receipt type origins. Those earlier results do
not substitute for a claimed fresh run.

Physical/production-provider/manual-wallet/mainnet acceptance remains explicitly
unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159). No software proof,
CI, security review or normal automatic testnet gate is deferred by that split.

## Current-main extension: SF-082

Main `39ea150a19a469c0edbb1b61ad822a80287a6f47` adds the separate
`contracts/fleet_data` package and owner-approved device archive retention.
The root implementation/proof sources and four prover gate inputs are unchanged
from `ac4417e`; the existing proofs retain that exact supported scope.
`check_move_security.py` now independently builds/lints/tests fleet_data too,
using its own committed framework/Walrus lock graph. This is not an assertion
that all three packages use the formal model's release framework revision.
Three function-local `prefer_mut_tx_context` allowances preserve already-published
fleet_data APIs. The independently stripped diagnostic baseline must match all
three exact warnings. No runtime signature/body/layout is changed by these
annotations. Real source mutations must make `revoked_health` and
`device_cannot_read` fail their expected-abort tests; a compilation failure is
not accepted as an authorization counterexample.

| File/functions | Reviewed boundary and coverage limit | Severity/status |
|---|---|---|
| `fleet_data.move`: `create`, `enroll`, `replace_account`, `owner`, `device` | Budget owner cap and budget/storage/registry/account/writer/device IDs are bound. Device writes require the active signer and live account via `assert_may_draw`; enrollment is owner-controlled, indexed once, and account replacement preserves sequences/cadence/history. | Source review and compiler/authority/adversarial tests; no complete formal proof of this separate Walrus-dependent package. |
| `report_health`, `anchor`, `request*` | Strictly increasing sequences, bounded payload/signature/hash sizes, per-stream cadence and shared emergency rate; certified non-deletable blobs must belong to this fleet/device and cannot be re-anchored. Device-provided health JSON/signatures and ciphertext digests still need client verification. | Intentional client authenticity/integrity boundary, not an on-chain plaintext or signature verification claim. |
| `set_support`, `rotate_key_epoch`, `seal_approve_*`, `identity_matches` | Current bearer owner cap controls policy; owner approval rechecks that cap. Support access is an explicit current account grant, with version/policy-ID/epoch/nonce binding. Devices/groups have no implicit decrypt grant. Support requests diagnostics within cadence but cannot spend through an inactive device. | Source review, epoch/support/revoked-key tests and an actual support-check omission control. Production Seal provider/credential acceptance remains #159. |
| `fleet_storage.move`: `configure_device_retention`, `clear_device_retention`, protected `register_blob` | Owner approves finite target/fee policy and revision; active device admission preserves quotas, checks live retention, authorizes/prepays its blob and checks global protected funding/capacity. Owner maintenance may reclaim expired protected slots. Failure rolls back object/resource changes; gas is still charged. | Source review and storage regression tests. No promise of indefinite history, perpetual availability or physical/provider recovery. |

## L-01 reachability and separate storage audit

The cross-registry counter example requires **two same-origin registries**.
A complete constructor search finds only private `device_registry::init`;
`init_for_testing` is test-only. A different package publication has a different
nominal registry type, and compatible upgrade does not rerun the initializer.
Thus the previously described example is an arbitrary/test-state accounting
limitation, not an established reachable production defect. The new
`test_two_test_only_registries_do_not_confer_identity_authority` deliberately
constructs that state, reproduces the counter mismatch and proves it confers no
active identity. Production source/clients do not use the registry count as
quota/authority. Do not infer a general origin binding or safety if a future
upgrade adds a second same-type registry constructor. Old-package code remains
callable; adding a new-function assertion alone would not change it.

The separate `fleet_storage` package is now explicitly compiled/linted/tested by
`check_move_security.py`, including its pinned Walrus dependency graph. Its one
unsuppressed lint is the intentional executor reward in `execute_renewal`:
changing the published return signature would break compatibility. The exception
is function-local and the gate independently requires the exact unsuppressed
baseline. Removing owner authorization is rejected by a genuine failing
`a_device_cannot_release_fleet_blobs` test. This is separate from the 11-module
main-package gate and is **not** a formal verification of Walrus/native behavior.

Source audit of `fleet_storage.move` found no confirmed new security defect:

| Functions | Checked boundary / limit |
|---|---|
| `assert_owner`, `create`, `migrate`, owner mutators | Budget ID and current bearer owner cap; normal writes enforce package version, migration only advances versions. Cap transfer delegates authority. |
| `register_blob`, `register_owner_impl`, `register_retained_fleet_blob` | Active signer/current account/budget checks; Walrus computes encoded size, pool split and actual fee. Period, size, rate and fee quotas abort atomically. Owner-approved retained uploads prepay their finite renewal resource. |
| `configure_retention`, `authorize_renewal`, `prepay_renewal`, `execute_renewal` | Owner-selected executor/revision, exact expected end epoch, finite target, live expiry/margin, prepaid resource and bounded gas reward; duplicates fail before a second payout. No arbitrary withdrawal/provider credential authority for executor. |
| `assert_retention_funded`, `assert_retention_live`, `withdraw_retention`, `release_blob` | Conservative fleet-blob closure, prepaid count/reserves and actual expiry checks; explicit owner withdrawal/release may reduce protection and emits risk evidence. They are owner powers, not a perpetual-availability guarantee. |
| `certify_blob`, `extend_blob`, storage resource operations | Delegate certificate/epoch/storage semantics to pinned Walrus contracts. Unit tests do not prove real Walrus certification, native correctness or production availability. |


## Authority map and findings

Severity means potential impact within the current client trust model. No
Critical/High defect was established in the current implementation by this
audit; absence of a finding is not proof of absence. Historical vulnerabilities
and current limitations are distinguished below.

| File/functions | Boundary and concrete assessment | Severity/status |
|---|---|---|
| `deployment_manager.move`: `make_deployment`, `receipts_mut`, `record_status`, `migrate`, `receipt_status*` | New records omit `StatusesKey`. Authoritative `ReceiptsKey` has its own introduction origin. Version 2 writers reject future generations and discard prior-generation statuses. Readers reject prior generations; legacy events are notifications, not evidence. Old packages remain callable. A sweep does not prevent old code creating fresh version-1 records, so every reader must check generation. | Historical High receipt forgery mitigated; adversarial unit/effects tests retained. No proof of all future upgrades. |
| `device_cap.move`: `issue`, `assert_signer`, `assert_active` | Public issuance does not grant impersonation: the cap's private UID holds its registered signer binding. Unbound legacy caps fail. Active reporting additionally needs live custody, unrevoked identity and current signer. Transferring a cap does not change its signer. | Checked by current tests, including operator impersonation, revocation and rotation failures. |
| `device_registry.move`: `activate_device`, `borrow_device`, `return_device`, `rotate_device_key`, `revoke_device`, `reinstate_device`, `deregister_activated_device` | `DeviceOwnerCap` is bearer authority. Custody plus a linear, non-droppable `DeviceLoan` prevents passing a Device by value to old deregistration code and committing without returning it. Return checks ID and signer. Rotation preserves revocation. Deregistration leaves a tombstone. The owner-cap holder can issue a new bound device cap through a temporary loan. | Current unit coverage; closed-writer custody representation now has the conditional model proofs described above; cross-version linkage uses real-chain regressions. Transferring/wrapping a bearer cap delegates its authority. |
| `device_registry.move`: `register_device`, `deregister_device` | **Finding L-01:** the registry counter is incremented/decremented through the supplied registry, but the legacy Device has no originating-registry binding. A holder of an unactivated Device can supply another registry with a nonzero counter and corrupt its accounting. With zero count the subtraction aborts atomically. This does not forge active custody or receipts. Do not use this count as an authorization/quota source. | **Informational/arbitrary-state limitation** after constructor reachability review above. Reproduced through the test-only repeated initializer; no reachable production exploit established. No new origin binding is claimed. Tracked in #82. |
| `enrollment.move`: `create_policy`, `add_claims`, `revoke_claim`, `set_limits`, `close`, `enroll` | Shared policy mutations check its operator. Enrollment binds registry, open flag, Clock expiry and quota; consumes a claim identified by gas sponsor, requires sponsorship and a distinct device sender, then atomically activates custody and transfers caps. Runtime signature validation, not a Move address comparison alone, establishes sponsor/device key possession. Aborts roll back claim/quota changes. | Existing positive/negative tests retained. Operator can deliberately re-add an already consumed claim address; “single use” means consumed until operator reauthorizes it. Provisioning must not recycle claim keys unintentionally. |
| `roles.move`: `allow_group`, `grant`, `revoke`, `is_active`, `create_deployment_as`; `deployment_manager.move`: `create_delegated` | Builder has no abilities and must be completed. Groups are checked against the immutable operator/admin. Shared registry activation is required on every delegated deployment; revocation checks recorded operator. Every target must be in scope; empty targets fail. Attributed creation is package-private. Delegate cannot use this API for direct devices or creator-only deletion. | Revocation/scope/unauthorized revoker/delete tests retained. Role is transferable bearer authority, not bound to the address logged in `RoleGranted`. A holder may further delegate via transfer/wrapping; do not interpret that event as a permanent signer ACL. |
| `device_group.move`: `add_device`, `remove_device`, `delete_group`; `deployment_manager.move`: `report_verified_status_for_group` | Shared group writes require admin. Group reporting requires targeted group ID, current membership and group admin equal to deployment creator. Enrollment/device custody does not make a group admin the device operator. Public `custodied_device` permits reading another operator's Device, so group membership alone is not operator consent. | Current targeting checks retained. Device operator authorization before apply remains a client/daemon boundary. |
| `deployment_manager.move`: `create_deployment`, `create_deployment_verified`, `report_verified_status`, `delete_deployment` | Legacy creation lacks a recipe digest and must remain unusable by secure clients. Verified creation validates a lowercase 64-byte recipe SHA-256 string, not Walrus bytes. Artifact digest strings in this legacy API are not format-validated on chain. Direct creation/reporting validates targeting and live signer, **not creator ownership of the device**; a receipt attests a device report, not independent proof of safe firmware/operator approval. Deletion checks creator. | Intentional client/daemon integrity and authorization boundary, not established by compiler/prover. Unknown/malformed hashes must fail before updater execution. |
| `component_catalog.move`: `publish_version`, `create_deployment`, `delete_deployment`, validators | Shared catalog updates require publisher. Name/version/platform/digest/token and vector consistency checks precede immutable version freezing. Duplicate versions/platform pairs fail. Deployment binding uses the immutable version ID; catalog deletion removes it through creator-checked package APIs. Choosing a version from another publisher is possible and must be an explicit operator trust decision. | Current catalog tests retained. Hash strings authenticate expected bytes only when clients actually verify downloads. No Walrus availability/certification proof. |
| `desired_state.move`: `begin`, `set_component`, `commit`; `deployment_manager.move`: `set_control` | Group admin/group ID/stale revision and authenticated recipe are checked; the non-droppable plan cannot escape its PTB. Creator controls schedule/state and cancellation is irreversible. | Source review and unit regressions; no exhaustive temporal/deployment-scheduling proof. |
| `fleet_handover.move`: `propose`, `confirm`, `step`, transfer steps, `finish` | Bounded unique inventory; independent recipient confirmation; source, expiry, kind and progress checks; group/deployment/role/enrollment operations retain underlying authority checks. Aborts roll back progress. Cancellation does not undo completed transfers. | Source review and unit regressions; no key recovery or atomic all-object transfer guarantee. |
| `fleet_budget.move`: owner/account guards, `draw_gas`, migration | Bound budget/cap/account IDs, current version, active signer/identity, owner-cap generation, pause/suspension and finite account reserve/period/draw limits. A compromised active key can spend its permitted allowance; funds already transferred cannot be recalled. | Source review and unit regressions; no formal SUI conservation/rate/Clock or future-upgrade proof. |
| `fleet_settings.move`: `set`, `new` (SF-130) | Only the current holder of the budget's `BudgetOwnerCap` (`fleet_budget::assert_owner`, which also gates the budget's package version) writes the fleet's single record in a budget dynamic field; contract bounds on every field; an older package cannot overwrite a newer record version. Every write emits the previous and new values. Devices only read it and may only run slower than it. | Source review, unit regressions and a localnet upgrade test with on-chain refusals (`make test-settings-localnet`); no formal proof. Cap transfer delegates authority. |
| `seal_policy.move`: `check_admin`, `identity_matches`, `*_allowed`, approvals | Bound bearer admin cap, code version, policy ID/epoch prefix and nonce bounds; device/group approvals require current signer and membership; group grants also require group admin. | Source review and unit regressions; external provider authentication/availability and cryptographic encryption are outside Move proofs. |

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
requires exactly the reviewed diagnostics in
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
