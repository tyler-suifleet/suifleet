# Native dynamic-field model continuation (SF-050)

SF-050 remains **blocked, not Review-ready**. Codex running **gpt-6.1-sol**,
reserved session `codex-move-security-20261015`, branch
`work/suifleet/sf-050-533dfe05`, continues [#82](https://github.com/tyler-suifleet/suifleet/issues/82)
and draft [PR #83](https://github.com/tyler-suifleet/suifleet/pull/83).
This is a concrete backend implementation following the independent findings
6358405907 and 6358436872, not a rerun of the unchanged old DOF model.

## Implemented representation and operations

The checksum-pinned `release-model.patch` changes the actual analysis, code
generator and native templates. Release application/framework bodies, public
layouts, type origins and UID cycle checks are unchanged.

Raw fields retain the shared occupancy/type-tag table per parent/Name, typed
value tables, unknown tags, exact missing/type/duplicate errors 1/2/0, and atomic
tag/value updates. Name-only observations retain complete carriers and encoders.
Existing/unproven-fresh UID rewrapping remains explicitly unsupported rather than
being assigned empty storage.

The new `object_field_module` replaces the old per-name DOF template:

- Analysis automatically collects the actual outer raw `Wrapper<Name> -> ID`
  projection, even without an explicit spec observer. Raw typed existence of
  that slot defines DOF `exists`; it does not require a particular child type.
- Child values are indexed by **child ID**, with separate owner and type metadata
  on the same parent carrier. An uninterpreted deterministic field-identity
  function abstracts the release hash of parent, Wrapper type and name. There
  is no injectivity/freshness/custody axiom.
- Add checks **any** raw outer occupancy and rejects duplicates with error 0.
  Successful add writes the outer ID/tag, child value, owner field identity and
  child type. It does not overwrite an independently occupied child ID.
- Borrow/remove resolve the outer ID to the child, checking owner, type and actual
  embedded child ID. Missing outer/child/owner gives error 1; wrong outer/child
  type gives error 2. Remove clears the child, its metadata, and outer ID/tag.
- Mutable borrow uses the child-ID path for writeback, preserving outer and
  metadata fields. `remove_opt` follows the release's **typed outer** existence
  guard: a wrong-typed outer slot returns none, unlike raw DF `remove_opt`.
- Struct updates, default constructors and both Pack paths preserve/initialize the
  added metadata. Arbitrary raw types, unknown child tags, orphan children and
  foreign owners are not excluded by an application custody precondition.

This is not a complete global heap refinement. The carrier-local child heap does
not yet establish input-object/heap disjointness or cross-carrier identity
uniqueness. Multiple object Name types on one carrier are rejected explicitly.
No claimed proof depends on these unsupported configurations.

## Namespace correctness boundary

The old `(Name,Value)` selectors dropped the raw/object kind. A shared Name could
alias physically independent fields or panic while assigning a raw tag to an
object-only Child type. The new analysis rejects overlapping raw/object Name
types **before code generation**, including same and different value types.
This is an unsupported-coverage rejection, not a security proof or solver-negative
control. Actual release-VM fixtures demonstrate that both configurations work and
that mutation/removal of one namespace preserves the other. Supporting their
proofs requires carrying kind through selectors, validity and mutable writeback;
compiler rejection is not counted as that proof.

## Comparison with the pinned runtime

The framework/runtime pin remains
`73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1`.

- [dynamic_object_field.move](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-framework/packages/sui-framework/sources/dynamic_object_field.move),
  `add_impl`, `borrow_impl`, `remove_impl`, `exists_with_type_impl`:
  raw Wrapper/ID first, child owned by that field UID second.
- [dynamic_field.move](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-framework/packages/sui-framework/sources/dynamic_field.move),
  `field_info`: typed outer lookup returns field UID and stored child address.
- [native dynamic_field.rs](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-move-natives/src/dynamic_field.rs),
  `add_child_object`: extracts the actual child's ID and type before insertion.
- [object_store.rs](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-move-natives/src/object_runtime/object_store.rs),
  `add_object:588–654`: an already-live child ID causes an invariant execution
  failure, rather than silently replacing its value. The prototype's
  `$ExecFailureAbort` collision branch preserves this failure. The source was
  read from the pinned Git object: blob `36d4ab4e61ec926c75a58a9da5badf0db3445684`,
  SHA-256 `6cc93ffde92d82171d208bb5d400b09b2fa1e364a73d13c482c9aa9875779964`.

This comparison does not prove native correctness, hashing collision resistance,
gas/resource bounds, storage effects or general heap simulation.

## Reproduction

Inside `suifleet-dev:sf004`, use the pinned `/tools` environment documented in
[MOVE_SECURITY.md](MOVE_SECURITY.md), including DOTNET_ROOT, BOOGIE_EXE and Z3_EXE.
Builds use locked dependencies and `CARGO_BUILD_JOBS=2`.

```bash
python3 scripts/build_move_prover.py --tools /tools
python3 scripts/check_move_native_vm.py
python3 scripts/check_move_native_proofs.py --tools /tools
python3 scripts/check_move_object_proofs.py --tools /tools
python3 scripts/check_move_proofs.py --tools /tools
python3 scripts/check_move_security.py
```

The fresh object roundtrip invokes the actual native DOF operations directly;
it does not assume the still-failing arbitrary-add contract. Each native negative
mutates exactly one emitted operation, then executes real Boogie/Z3. The gate
requires the intended Check failure/abort code and successful Assume and
SpecNoAbortCheck; parser errors, empty runs and timeouts fail the gate.

## Current results and remaining blockers

Commands ran network-none in the pinned image, on unchanged main
`5137be97ef273848dec1df81a433c0ec5454effb`. `git fetch origin main` confirmed no
new base. Actual results:

| Command | Result |
|---|---|
| Locked offline Rust build | Pass; final incremental native-template build 37.26s |
| `build_move_prover.py --tools /tools` | Pass; exact patch/overlay/lock pins accepted, Boogie 0 warnings/errors |
| `check_move_native_vm.py` | **18/18 pass**: retained 12 plus same/different-value raw/object noninterference, wrong requested Child (2), different-type duplicate (0), missing field (1), remove/re-add/mutation/name/parent framing |
| `check_move_native_proofs.py --tools /tools` | **24 phase obligations/3 intended solver negatives pass**; existing-UID rejection remains explicitly unsupported |
| `check_move_object_proofs.py --tools /tools` | **3 fresh-roundtrip phase obligations/5 intended solver negatives pass**: omit outer, omit child, skip clear, wrong owner, wrong ID; same-Name overlap rejected before code generation, not proved |
| `check_move_proofs.py --tools /tools` | **63 phase obligations/25 intended solver negatives pass**, all retained |
| `check_move_security.py` | Pass: strict root/storage compiler and linters, reviewed unsuppressed diagnostics, compiler/linter negatives, **169 root/53 storage tests**, authority-removal negatives |
| Python parsing, actionlint 1.7.7, compatibility SHA-256 pins, `git diff --check` | Pass |
| Tracking tests and dry run | Initial failure exposed the missing existing SF-050 seed. After restoring its authoritative specification and verified issue/Project IDs separately: **10/10 pass**, 85-issue offline plan passes. SF-087 was restored only from the supplied seed; no new issue or dependency waiver. |

Essential failing obligations, **not deferred external acceptance**:

1. `--include-object-fixtures --functions object_add_slots_spec`: Check fails
   **2.0s** at actual add, while Assume/SpecNoAbortCheck pass. Raw absence no
   longer conflicts with an independent per-name table. Instead the input heap
   can already contain the incoming child's ID. Its owner/tag state remains
   possible; no input-custody/freshness assumption excludes it.
2. `--include-object-fixtures --functions object_frame_spec --timeout 90`:
   concrete Check failures **21.4s** at the second/different-name add after
   supported VC splitting. Assume/SpecNoAbortCheck pass. The initial unsplit
   30s timeout was not accepted as a counterexample. The upstream
   `packages/sui-specs/sources/sui-framework/tx_context.move::fresh_object_address_spec`
   preserves only context digest and leaves its returned address unconstrained;
   distinct `object::new` calls may therefore produce the same child ID in the
   model. The release VM's same fixture passes. Fixing this requires a faithful
   transaction/object-identity model and construction/preservation evidence;
   adding a freshness axiom or silently overwriting a child would hide the gap.
3. `--include-pending-custody --functions activate_device_security_spec return_device_security_spec`:
   both Checks fail (**6.8/7.7s**) at native add. Activation Assume also fails
   (**1.9s**) because raw outer absence is stronger than arbitrary incoming
   registry state; return Assume and both SpecNoAbortChecks pass.
4. `--include-pending-enrollment --functions enroll_security_spec --timeout 180`:
   Check fails with concrete diagnostics (**145.3s**), remaining phases pass.
   Actual init/activation/return/enrollment inductive preservation and the
   identity-omission controls remain required, not completed by the fresh fixture.

No language-level impossibility or executable SuiFleet exploit is established.
The concrete remaining work is identity/global-heap refinement and complete
closed-writer induction, retaining unknown/orphan/foreign states until genuinely
excluded. Existing-UID reconstruction also needs heap-preserving support.

There are no changed release application sources/manifests/locks versus `f941092`.
Prior byte-array and 35/47 local-chain effects evidence remains historical; no new
release-array/chain-effects run is claimed here. Hosted exact-head CI, automatic
testnet and independent review must still be recorded. PR #83 stays draft and the
claim remains reserved. Physical/provider/manual-wallet/mainnet acceptance remains
unrun in #32/#159; it does not cover any software proof gap above. No wallet,
production credential, hardware programming or mainnet mutation was used.
