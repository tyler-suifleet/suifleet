# Native dynamic-field model continuation (SF-050)

SF-050 remains **blocked**, not ready for Review. This continuation implements
and tests the raw-field tag-table milestone. It does not complete the object-field
heap refinement or the custody/enrollment/init proofs. Model: **gpt-6.1-sol**;
reserved session `codex-move-security-20261015`, branch
`work/suifleet/sf-050-533dfe05`, issue [#82](https://github.com/tyler-suifleet/suifleet/issues/82),
draft [PR #83](https://github.com/tyler-suifleet/suifleet/pull/83).

## Implemented raw-field representation

The checksum-pinned `release-model.patch` extends the actual backend rather than
replacing application bodies. For each parent carrier and canonical Name type,
there is one `Table int int` of value-type tags, including Name-only observations.
The existing typed value tables remain. Name existence is defined by tag-table
membership. `$IsValid` relates each modeled typed table's membership to occupied
name plus its corresponding tag. This is representation consistency, not a
SuiFleet custody/authority invariant.

Tags enumerate distinct resolved Move `Type` identities across the analysis;
renamed imports resolve to the same Type. They do not use the Boogie primitive
representation (for example, different integer widths must remain distinct Move
types). Unresolved generic value types are rejected rather than assigned
potentially unsound distinct tags. Occupied slots may have arbitrary integer tags
outside the observed set. No finite-type exhaustiveness axiom is added.

The raw native templates now:

- Reject add on **any** name occupancy, including an unknown/wrong value type;
  atomically update the tag and selected typed table on success.
- Check typed membership on borrow, mutable borrow and remove. Distinguish missing
  field (1), wrong type (2), and duplicate add (0), matching the release API.
- Remove both the typed value and shared tag. `remove_opt` aborts on an occupied
  wrong type instead of incorrectly returning none.
- Preserve tag fields in ordinary field updates and typed mutable writeback.
  Both Pack paths and default constructors carry the additional fields.
- Emit key encoders and full carrier datatypes for **Name-only** observations,
  even when no value projection is modeled. The new independent Name-only fixture
  caught missing encoder/opaque-carrier defects during implementation; both were
  fixed before accepting its proof.

The carrier's initial empty tables are accepted only through a conservative
straight-line fresh-UID provenance check. An existing or unproven-fresh UID,
control-flow ambiguity, nested/non-leading UID carrier, or unknown constructor
fails condition generation instead of gaining an empty-store assumption. The
pinned framework's private `object::new`/`new_uid_from_hash` constructors retain
their native identity boundary. This is a limited supported constructor analysis,
not a general address-indexed heap preservation implementation. Raw and object
fields using the same Name type are rejected because the old shared typed-table
representation cannot distinguish their physical slots.

## Comparison to actual release sources

Source inspection used Git objects at framework pin
`73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1`, without modifying release bodies:

- [dynamic_field.move](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-framework/packages/sui-framework/sources/dynamic_field.move):
  `add` checks untyped `has_child_object` before creating `Field<Name,Value>`;
  borrow/remove use typed child lookup. Removing destroys the Field UID.
- [native dynamic_field.rs](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-move-natives/src/dynamic_field.rs):
  `hash_type_and_key` passes parent, Name's type tag and serialized Name to
  `derive_dynamic_field_id`; it does not include Value's type. Typed child loading
  rejects mismatches, and removal moves the stored child out.
- [sui-types dynamic_field.rs](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-types/src/dynamic_field.rs):
  the derived child ID hashes parent, encoded Name length/bytes and Name type.
  The model's separate typed-name domains abstract this addressing; it does not
  prove cryptographic collision resistance, native correctness or gas behavior.
- [dynamic_object_field.move](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-framework/packages/sui-framework/sources/dynamic_object_field.move):
  `add_impl` stores raw `Wrapper<Name> -> ID`, then adds the actual child under
  the Field UID. `exists` checks the typed outer ID field; `exists_with_type_impl`
  also checks the child. Raw occupancy alone is not object-field membership.

Actual executable conformance uses the pinned checksum-verified CLI/Move VM
`sui 1.81.1-e04cf9aa915c`, independently of these prover templates. No wallet keys,
network mutations or application release bodies are needed.

## Reproduction and controls

Use the pinned image and existing tools root as in [MOVE_SECURITY.md](MOVE_SECURITY.md).
Inside the image:

```bash
CARGO_BUILD_JOBS=2 python3 scripts/build_move_prover.py --tools /tools
python3 scripts/check_move_native_vm.py
python3 scripts/check_move_native_proofs.py --tools /tools
python3 scripts/check_move_proofs.py --tools /tools
```

The VM fixtures cover fresh UID, typed/untyped existence, correct read, mutable
writeback, wrong-type borrow/remove/remove_opt, same/different-type duplicate add,
remove/re-add with another value type, parent isolation, untouched key and Name
type framing, an unmodeled occupied type and a Name-only observer. They also
exercise legal non-key UID rewrapping and a real object-field add/borrow/remove.

The raw prover gate requires all three phases for eight actual-function targets
(**24 obligations**). It then executes real Boogie/Z3 on three deliberate
controls: missing tag clear, wrong-type alias, and the false assertion that an
occupied unknown-type slot is impossible. The arbitrary-input borrow and remove_opt specifications additionally require
exact missing/wrong-type abort boundaries, including wrong-type remove_opt
instead of a successful none result. Each control must fail exactly its intended
Check/postcondition while Assume and SpecNoAbortCheck pass. The first two mutate
only the emitted native transition/function in a guarded Boogie wrapper; the
third mutates only the temporary specification. No production proof uses those
controls. A parser/model error, timeout or unsuccessful positive fails the gate.

The separate constructor rejection is explicitly **not a verified property or
solver counterexample**. The real VM accepts a non-key `Parent { id: UID }` whose
UID is unpacked and repacked, preserving its field. A carrier-local empty-Pack
model would discard that store. The current checker rejects that operation with
`packing an existing or unproven-fresh UID would discard its child store`.
Key-bearing rewrapping is independently rejected by Sui's object-construction
verifier; the non-key fixture is necessary to demonstrate the actual limitation.

CI runs both the VM/native gate and the application 63-obligation /
25-negative gate, preserving all prior 24 controls and adding a deliberately
inserted legacy-status field. The stronger role name-removal and fresh legacy-status absence
obligations pass and belong to the application specifications, separately from the native
model implementation. Hosted CI remains an exact-head gate, never inferred from
local success.

## Unmet object-field and application scope

The opt-in `object_add_slots_spec` uses exact spec-only Wrapper construction and
macros expanded at the caller. Generated artifacts contain **the same Parent
carrier** for the raw outer Wrapper/ID/tag tables and UID-bearing Child table;
`Parent -> Child -> UID` is well founded and the UID cycle detector stays enabled.
This concrete prototype gets through model construction and reaches the solver.
Check and Assume fail: empty raw outer occupancy does not prevent the old DOF
model's disconnected typed-table duplicate abort, and adding a Child does not
establish the actual outer typed ID/value postconditions. SpecNoAbortCheck passes.
The actual VM object's roundtrip passes. These are model counterexamples, not an
established executable SuiFleet exploit.

```bash
python3 scripts/evaluate_move_prover.py --prover-source /tools/sui-prover \
  --framework-packages /tools/framework \
  --prover-binary /tools/sui-prover/target/release/sui-prover \
  --include-object-fixtures --functions object_add_slots_spec \
  --artifacts /tools/NEW_OBJECT_ARTIFACT_DIRECTORY
```

A safe continuation must replace the old DOF projection with both actual slots:
outer Field identity/Wrapper/ID plus child heap keyed by that Field UID and
referenced child ID, checking child identity and ownership. It must retain wrong
outer types, unknown child types and orphan states until construction/preservation
proves otherwise. Equating raw-wrapper presence and Child membership, or requiring
custody validity on arbitrary inputs, would hide the failing obligations.

Activation/return now use the caller-expanded raw-name observer. Their actual
Check/Assume obligations still fail. Full enrollment also remains a concrete
Check failure; construction/init and all custody writer preservation and the
omitted-identity/incorrect-ID controls remain required. They cannot be labeled
external acceptance or handed to Review. Complete this heap refinement and prove
those actual writers before requesting independent full-scope approval.

No new release arrays/layouts/type origins, physical results, public-network
acceptance, mainnet writes, production-provider sessions or wallet acceptance are
claimed by this continuation. Earlier 169/53 contract tests and 35/47 local-chain
effects remain historical evidence, not rerun results here.

## Results for this continuation

All commands below ran in `suifleet-dev:sf004`; prover/solver paths and environment
are the pinned `/tools` layout above. Local build used `CARGO_BUILD_JOBS=2` and
locked dependencies. No shared-wallet mutation or physical operation occurred.

| Command | Actual result |
|---|---|
| `cargo build --offline --locked --release -p sui-prover` after final Name-only correction | Exit 0, 1m01s; prior tag/guard/key-encoder incremental builds also succeeded |
| `python3 scripts/build_move_prover.py --tools /tools` | Exit 0 on cached tools; source-overlay/lock/archive checks and pinned Boogie build succeeded (0 errors) |
| `python3 scripts/check_move_native_vm.py` | Exit 0, **12/12 tests** |
| `python3 scripts/check_move_native_proofs.py --tools /tools` | Exit 0, **24 positive phase obligations**, **3 intended solver-negative controls**; existing-UID reconstruction explicitly rejected as unsupported |
| `python3 scripts/check_move_proofs.py --tools /tools` | Exit 0, **63 positive phase obligations**, **25 intended solver-negative controls**, preserving all earlier 24 |
| Opt-in `--include-object-fixtures --functions object_add_slots_spec` | Exit 1; Check/Assume fail in 0.7/0.7s; SpecNoAbortCheck passes |
| Opt-in `--include-pending-custody --functions activate_device_security_spec return_device_security_spec` | Exit 1; both Checks fail (1.2/0.9s), both Assumes fail (1.1/1.0s); both SpecNoAbortChecks pass |
| Opt-in `--include-pending-enrollment --functions enroll_security_spec --timeout 180` | Exit 1; concrete Check failure **34.4s**; Assume and SpecNoAbortCheck pass |
| Python compilation of six changed build/evaluation/gate/control scripts | Pass |
| `actionlint 1.7.7 .github/workflows/move-security.yml` | Pass |
| Compatibility SHA-256 pins, local documentation links and `git diff --check` | Pass |
| Comparison of release source/manifests/lockfiles with previous head `c0279c7` | No changes; no new release-bytecode build or chain-effects run claimed |

The first legacy-status omission control timed out at 30s; that was **not**
accepted as a negative result. Supported bounded VC splitting on the actual
constructor spec produced the intended absence-postcondition counterexample in
1.4s, and the complete application gate subsequently passed with that control.
`rustfmt` was unavailable in the pinned image/host toolchain; no formatting-tool
success is claimed. This is not a configured gate. Hosted CI/testnet and fresh
independent review are not established by any local result in this table.
