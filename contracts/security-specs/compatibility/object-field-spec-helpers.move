    #[mode(spec), ext(spec(prove, target = add, no_opaque))]
    fun object_add_slots_spec(parent: &mut Parent, name: u64, child: Child) {
        let id = object::id(&child);
        let payload = child.payload;
        prover::prover::asserts(!dof::spec_name_exists_macro!(&parent.id, name));
        add(parent, name, child);
        // These observe the real outer Wrapper<Name> -> ID, independently of Child.
        prover::prover::ensures(dof::spec_outer_typed_macro!(&parent.id, name));
        prover::prover::ensures(dof::spec_outer_id_macro!(&parent.id, name) == id);
        prover::prover::ensures(dof::exists_with_type<u64, Child>(&parent.id, name));
        prover::prover::ensures(object::id(dof::borrow<u64, Child>(&parent.id, name)) == id);
        prover::prover::ensures(dof::borrow<u64, Child>(&parent.id, name).payload == payload);
    }

    #[mode(spec), ext(spec(prove, target = roundtrip, boogie_opt = b"vcsMaxKeepGoingSplits:2 vcsSplitOnEveryAssert vcsFinalAssertTimeout:30"))]
    fun object_roundtrip_spec(ctx: &mut TxContext) { roundtrip(ctx); }

    #[mode(spec), ext(spec(prove, target = remove_readd_and_frame, boogie_opt = b"vcsMaxKeepGoingSplits:2 vcsSplitOnEveryAssert vcsFinalAssertTimeout:30"))]
    fun object_frame_spec(ctx: &mut TxContext) { remove_readd_and_frame(ctx); }

    // Direct native-model conformance. The bounded starting counter excludes
    // u64 overflow, which is outside this fixture's allocation budget.
    #[mode(spec), ext(spec_only)]
    fun allocation_observations(ctx: &mut TxContext) {
        let legacy = sui::tx_context::spec_legacy_counter(ctx);
        let before = sui::tx_context::spec_native_counter();
        if (before >= 18446744073709551613) return;
        let first = sui::tx_context::fresh_object_address(ctx);
        assert!(sui::tx_context::spec_native_counter() == before + 1, 171);
        assert!(sui::tx_context::spec_last_created() == first, 172);
        assert!(sui::tx_context::spec_last_created() == first, 173);
        assert!(sui::tx_context::spec_native_counter() == before + 1, 174);
        let second = sui::tx_context::fresh_object_address(ctx);
        assert!(sui::tx_context::spec_native_counter() == before + 2, 175);
        assert!(first != second, 176);
        assert!(sui::tx_context::spec_last_created() == second, 177);
        assert!(sui::tx_context::spec_legacy_counter(ctx) == legacy, 179);
    }
    #[mode(spec), ext(spec(prove, target = allocation_observations))]
    fun allocation_model_spec(ctx: &mut TxContext) {
        allocation_observations(ctx);
    }

    #[mode(spec), ext(spec(prove, target = allocation_distinct))]
    fun allocation_distinct_spec(ctx: &mut TxContext) { allocation_distinct(ctx); }

    #[mode(spec), ext(spec(prove, target = remove_and_readd))]
    fun object_local_ownership_spec(parent: &mut Parent, name: u64) {
        prover::prover::asserts(dof::exists_with_type<u64, Child>(&parent.id, name));
        let id = object::id(dof::borrow<u64, Child>(&parent.id, name));
        remove_and_readd(parent, name);
        prover::prover::ensures(dof::exists_with_type<u64, Child>(&parent.id, name));
        prover::prover::ensures(object::id(dof::borrow<u64, Child>(&parent.id, name)) == id);
    }
    #[mode(spec), ext(spec(prove, target = take))]
    fun object_return_ownership_spec(parent: &mut Parent, name: u64): Child {
        prover::prover::asserts(dof::exists_with_type<u64, Child>(&parent.id, name));
        let id = object::id(dof::borrow<u64, Child>(&parent.id, name));
        let child = take(parent, name);
        prover::prover::ensures(object::id(&child) == id);
        prover::prover::ensures(!dof::exists(&parent.id, name));
        child
    }
