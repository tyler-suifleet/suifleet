# Sui native and VM model boundary (SF-050)

This prototype follows the pinned release source and supports the actual registry's raw/DOF storage
shape and closes the earlier failing construction/enrollment obligations. It is
conditional verification, **not a proof of the Sui Rust runtime**. Independent
review and exact-head CI remain required. Actual running model: **gpt-6.1-sol**;
issue [#82](https://github.com/tyler-suifleet/suifleet/issues/82), PR
[#251](https://github.com/tyler-suifleet/suifleet/pull/251) (recovered from #83).

Every backend/template/spec overlay is pinned by SHA-256 in
`contracts/security-specs/toolchain.json`. `release-model.patch` applies to the
pinned Asymptotic source before compilation. No executable release framework or
application body, layout, type origin, or UID cycle check is removed/replaced.
Proof-only helpers expose exact observations, not trusted application behavior.

## Native allocation: explicit cryptographic trust boundary

At Sui revision `73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1`:

- `tx_context.move:65–68` ignores the legacy Move context field and calls native
  `fresh_id`. Rust `TransactionContext::fresh_id` derives the returned ID from
  the **native transaction digest and old native counter**, then increments the
  native counter. `fresh_id` also registers that ID in ObjectRuntime.
- Native `native_ids_created` reads that same counter. `last_created_id` returns
  `ObjectID::derive_id(digest, counter - 1)` without incrementing it; zero count
  is an execution failure. Repeated observations return the same ID.
- `ObjectID::derive_id` hashes the RegularObjectId domain, digest and little-endian
  counter. The Rust code does **not** check/retry collisions. Counter uniqueness
  alone is not a mathematical proof of distinct hash outputs.

The model has separate native digest/counter/allocation-history state and one
uninterpreted deterministic derivation function. The actual Move wrapper executes
its actual body. The counter getter, last-created getter and derivation use this
same state/function. Legacy Move `ids_created` is unchanged, as demonstrated by
both formal observations and a real-VM before/after BCS comparison.

**MODEL TRUST BOUNDARY:** for an unused native allocation tuple, the documented
Sui globally-unique fresh-object contract is treated as a collision-free
cryptographic abstraction against prior allocated/live identities. This is a
scoped assumption at native allocation, never an application precondition, a
universal hash-injectivity axiom, or a claimed Rust collision check. An allocation
high-water mark records the contiguous tuple history; the starting native counter
accounts for calls before the verified function. A repeated/frozen counter does
not obtain another freshness assumption. Counter-freeze and returned-ID-reuse
controls therefore produce genuine distinctness counterexamples rather than an
unsatisfiable path. The counter-observation fixture explicitly branches around
u64 overflow; gas, counter exhaustion and protocol allocation limits are not
proved by this model.

`object::record_new_uid` and `new_uid_from_hash` do **not** call this fresh
allocator model. Rust ObjectRuntime::new_id only inserts into sets and explicitly
allows deleting and re-adding a hash-derived Field with the **same ID**. No
freshness assumption applies to that lifecycle. Hash-based field owner identity
is deterministic and uninterpreted, with no injectivity assertion.

Primary source comparison:
[Move context](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-framework/packages/sui-framework/sources/tx_context.move),
[native context](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-move-natives/src/tx_context.rs),
[TransactionContext and ObjectID](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-types/src/base_types.rs),
[ObjectRuntime::new_id](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-move-natives/src/object_runtime/mod.rs).

## General VM admission and linear resources

Sui's transaction checker rejects ObjectOwner objects as normal inputs
(`sui-transaction-checks/src/lib.rs:590`). The v3 PTB loader repeats that check
and rejects duplicate input IDs (`context.rs:1817,1857–1859`). Its
`by_value_arg:454–545` consumes a noncopy value from its Option and rejects
borrowed/consumed values; mutable borrowing is exclusive. ObjectRuntime final
ownership checks reject cycles. The ID-leak verifier's Fresh marker is constructor
provenance; it is not physical hash freshness and includes hashed constructors.

The supported model projects these VM facts onto rooted carriers and by-value
key objects. At verification entry only, local objects are disjoint from live
children and other input roots. The native allocation-history projection reserves
existing root/child identities. A rooted carrier cannot simultaneously be its own
stored child; this is generic resource well-formedness, not private registry
custody typing. Native add consumes local ownership through Move's real noncopy
argument semantics and installs child ownership; remove clears it and produces a
local value; borrow retains stored ownership.

Internal calls **assert** argument separation; actual implementation returns
**assert** result separation. They do not re-assume VM admission. Ordinary modular
summaries expose checked output separation as a postcondition; the corresponding
actual-function Check must pass. SpecNoAbortCheck intentionally havocs results to
check specification well-definedness and does not re-check program preservation
on those synthetic results. The native missing-remove-owner mutation fails the
actual Check/Assume return obligations. It cannot be hidden by call admission.
The separate remove/re-add caller fixture also discharges internal-call checks.

UIDs abstracted by upstream as opaque identity tokens retain that representation;
concrete address observations use concrete UID/ID fields. Non-leading/nested key
UID layouts are rejected explicitly. This is the supported local resource/heap
projection, not an implementation or proof of the entire VM, all recursive
ownership graphs, arbitrary PTBs or package admission.

Primary sources:
[input checker](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-transaction-checks/src/lib.rs),
[PTB context](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/v3/sui-adapter/src/programmable_transactions/context.rs),
[ID-leak verifier](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-verifier/src/id_leak_verifier.rs).

## Shared raw and two-slot object storage

Raw fields have one occupancy/type-tag map per parent/Name plus typed value maps.
Unknown tags remain possible. Add checks any occupancy; missing/type/duplicate
errors remain 1/2/0; tag and typed values change atomically. Name-only observations
retain their complete carrier and canonical Move-type tag identity.

DOF uses the actual raw `Wrapper<Name> -> ID` outer slot, plus child-ID-indexed
values, owner and type metadata on the same parent carrier. Typed outer presence
alone defines `exists`. Add checks any outer occupancy, writes its ID/tag and
installs the child/owner/type; independently occupied child IDs cause an execution
failure. `object_store.rs:620–635` uses UNKNOWN_INVARIANT_VIOLATION_ERROR for that
condition, **not** MoveAbort EFieldAlreadyExists. The model's `$ExecFailureAbort`
uses the execution-failure category (-1), preserving this distinction from 0.

Borrow/remove resolve outer ID, owner field identity, type and actual child UID.
Missing outer/child/owner gives 1, wrong type gives 2. Remove clears both physical
slots and metadata; mutable borrow writes the child-ID path. `remove_opt` has the
release's typed-outer existence guard. Other parents/names/children are framed.
Unknown tags, orphan children and foreign owners are retained in arbitrary native
fixtures, except the general VM resource constraints above.

The quantified private registry invariant is **separately established and
preserved**, as documented in [MOVE_SECURITY.md](MOVE_SECURITY.md). It is not
silently installed in generic native validity. The ordinary arbitrary-state gate
runs without it. Constructor wrong-type, activation identity omission, return
custody omission and enrollment claim/quota omission controls fail actual proofs.

Primary comparison:
[DOF wrappers](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-framework/packages/sui-framework/sources/dynamic_object_field.move),
[raw field_info](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/crates/sui-framework/packages/sui-framework/sources/dynamic_field.move),
[native child insertion](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-move-natives/src/dynamic_field.rs),
[child store](https://github.com/MystenLabs/sui/blob/73dd2c2ba6f9fdb21d7ffde2b50a3f2f0ac39bc1/sui-execution/latest/sui-move-natives/src/object_runtime/object_store.rs).

## Explicit unsupported shapes and controls

- Existing-UID carrier rewrapping needs heap-preserving projections. It fails
  condition generation rather than receiving empty tables.
- `new_uid_from_hash` is no longer exempt when UID itself carries projections.
  The dedicated fixture creates a child, deletes the UID and reconstructs the
  **same UID address through the hashed constructor**; the actual hashed constructor Pack is
  rejected. This is a compatibility rejection, not a proved reconstruction.
  No release public hashed-constructor API is added.
- Raw/DOF use of the same Name type is explicitly rejected before code generation
  to prevent selector aliasing. Real VM fixtures demonstrate these namespaces
  are distinct; their successful VM behavior is not a claimed supported proof.
- Multiple DOF Name types on one carrier and unsupported nested/non-leading UID
  projections fail closed. Actual DeviceRegistry/CustodyKey/Device uses the
  supported shape. All upstream UID-cycle checks remain enabled.

Release Move unit-VM execution is distinct from Sui package/transaction admission.
In particular a reconstruction unit test is not evidence that Sui's ID-leak
verifier admits that package. These unsupported shapes are outside the actual
critical writer scope; none is counted as a formal positive/negative theorem.

The reproducible gates are in [MOVE_SECURITY.md](MOVE_SECURITY.md): 63/25
arbitrary application, 24/3 raw, 21/9 allocation/DOF/ownership and 42/5 closed-writer
phases/negatives. Nineteen real release-VM fixtures cover allocation observations,
raw/native failures, parent/name framing, remove/re-add, mutable child updates,
unknown tags, wrong child type and raw/DOF namespace independence. Exact final
run/CI/effects evidence belongs in #82/PR #83; native source inspection and these
finite controls do not prove native Rust, cryptography or complete application
security. No production/mainnet/hardware operation is part of this model work.
