    #[mode(spec), ext(spec(prove, target = add))]
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

    #[mode(spec), ext(spec(prove, target = roundtrip))]
    fun object_roundtrip_spec(ctx: &mut TxContext) { roundtrip(ctx); }

    #[mode(spec), ext(spec(prove, target = remove_readd_and_frame, boogie_opt = b"vcsMaxKeepGoingSplits:2 vcsSplitOnEveryAssert vcsFinalAssertTimeout:30"))]
    fun object_frame_spec(ctx: &mut TxContext) { remove_readd_and_frame(ctx); }
