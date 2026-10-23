
// Proof-only observations call the pinned native accessors, not legacy fields.
#[mode(spec), ext(spec_only)]
public fun spec_native_counter(): u64 { native_ids_created() }
#[mode(spec), ext(spec_only)]
public fun spec_last_created(): address { last_created_id() }
#[mode(spec), ext(spec_only)]
public fun spec_legacy_counter(ctx: &TxContext): u64 { ctx.ids_created }
