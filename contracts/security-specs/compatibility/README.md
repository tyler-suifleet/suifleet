# Release-framework compatibility overlay

This overlay targets only the prover/toolchain revisions in `../toolchain.json`.
It is not executable contract code and is not a verified security theorem.

Apply `release-model.patch` to a clean checkout of the pinned `sui-prover` commit
**before building the binary**:

```bash
git -C "$TOOLS/sui-prover" apply "$PWD/contracts/security-specs/compatibility/release-model.patch"
(cd "$TOOLS/sui-prover" && CARGO_BUILD_JOBS=2 cargo build --locked --release -p sui-prover)
```

The patch changes three locations:

- `move-model/src/model.rs` recognizes release `sui::object::record_new_uid`.
  Optional function lookup also checks that the declaration exists, using the
  upstream `get_existing_fun_qid_opt` helper. The original lookup manufactured
  IDs for absent fork helpers such as `dynamic_field::borrow_or_unknown`, then
  panicked in backend name generation. Absent optional functions receive no
  model; real release functions retain their upstream models. No semantics are
  invented for a missing helper.
- The object spec wrapper calls that actual one-argument release native. It does
  not substitute a successful body or the fork's two-argument native.
- The funds spec retains the release add/withdraw wrappers and removes only the
  wrapper/import for `reserve_object_funds_for_withdrawal`, which does not exist
  in the pinned release. No release function is deleted or reimplemented.

The evaluator hashes the original release framework before staging it. It adds
`integer.move` only in the temporary proof framework. The declaration matches
`std::integer::Integer` in compiler fork `a30b3efcfaa333241c1864581753128eedc5dbf1`
(`mode(spec)`, `ext(spec_only)`, native, copy/drop/store). It adds **no arithmetic
functions, axioms or replacement executable bodies**. Its purpose is to resolve
the datatype imported by the prover's vector specifications. This does not
establish arithmetic modeling for release programs.

Both overlay files have SHA-256 pins in `../toolchain.json`. The evaluator
requires exactly the committed patch in the prover source and rejects extra
tracked edits or untracked source/model files. A binary must be rebuilt from
that patched source; input checks alone do not authenticate an arbitrary binary.
The UID cycle detector and all other upstream analyses remain enabled.

Run the focused actual-implementation experiment with the pinned Boogie/Z3
configuration from `docs/MOVE_SECURITY.md`:

```bash
python3 scripts/evaluate_move_prover.py \
  --prover-source "$TOOLS/sui-prover" \
  --framework-packages "$TOOLS/framework" \
  --prover-binary "$TOOLS/sui-prover/target/release/sui-prover" \
  --functions close_spec set_limits_spec
```

Negative controls use `--mutation close-authority`, `close-state`,
`limits-authority` or `limits-quota` with the corresponding `--functions` filter.
They edit only a disposable copy of the actual implementation. A negative is
valid verification evidence only after the unmodified positive passes, and only
when the solver rejects the intended obligation: a parser/model/tool failure is
not an invariant counterexample. Zero quota is valid in the implementation;
`limits-quota` must fail equality to an arbitrary caller-supplied quota, rather
than a newly invented nonzero-quota requirement.

`boogie-locks/` contains the 14 runtime NuGet lockfiles produced from the pinned BoogieDriver
project graph. Their ordered path/content hash is pinned in `toolchain.json`.
`scripts/build_move_prover.py` verifies this hash and restores with
`--locked-mode`, then builds with `--no-restore`. Cargo uses the pinned lockfile.
The SDK and Z3 archives are independently checksum-verified. The normal CI gate
runs `scripts/check_move_proofs.py`; it requires all six positive obligations and
exactly the intended solver obligation/counterexample for each mutation. A
parser failure, model panic, timeout or unsuccessful positive fails the gate.
