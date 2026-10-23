module sf050_native::raw_field_specs;
use sf050_native::raw_fields;
use sui::dynamic_field as df;
use prover::prover::{asserts, ensures};

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::fresh_roundtrip))]
fun raw_roundtrip_spec(ctx: &mut TxContext) { raw_fields::fresh_roundtrip(ctx); }

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::foreign_parent))]
fun raw_foreign_parent_spec(ctx: &mut TxContext) { raw_fields::foreign_parent(ctx); }

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::add_to_arbitrary))]
fun raw_add_spec(parent: &mut UID, key: u64, value: u64) {
    asserts(!df::exists(parent, key));
    raw_fields::add_to_arbitrary(parent, key, value);
    ensures(df::exists(parent, key));
    ensures(df::exists_with_type<u64, u64>(parent, key));
    ensures(!df::exists_with_type<u64, bool>(parent, key));
    ensures(*df::borrow<u64, u64>(parent, key) == value);
}

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::remove_arbitrary))]
fun raw_remove_spec(parent: &mut UID, key: u64): u64 {
    asserts(df::exists_with_type<u64, u64>(parent, key));
    let result = raw_fields::remove_arbitrary(parent, key);
    ensures(!df::exists(parent, key));
    ensures(!df::exists_with_type<u64, u64>(parent, key));
    ensures(!df::exists_with_type<u64, bool>(parent, key));
    result
}

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::unknown_occupancy))]
fun raw_unknown_spec(parent: &UID, key: u64): bool {
    let result = raw_fields::unknown_occupancy(parent, key);
    ensures(result == (df::exists(parent, key)
        && !df::exists_with_type<u64, u64>(parent, key)
        && !df::exists_with_type<u64, bool>(parent, key)));
    result
}

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::name_only_occupied))]
fun raw_name_only_spec(parent: &UID, nonce: u64): bool {
    let result = raw_fields::name_only_occupied(parent, nonce);
    ensures(result == df::exists(parent, raw_fields::name_only_key(nonce)));
    result
}

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::borrow_bool))]
fun raw_borrow_spec(parent: &UID, key: u64): bool {
    asserts(df::exists_with_type<u64, bool>(parent, key));
    let value = *df::borrow<u64, bool>(parent, key);
    let result = raw_fields::borrow_bool(parent, key);
    ensures(result == value);
    result
}

#[mode(spec), ext(spec(prove, target = sf050_native::raw_fields::remove_bool_opt))]
fun raw_remove_opt_spec(parent: &mut UID, key: u64): Option<bool> {
    let present = df::exists(parent, key);
    let typed = df::exists_with_type<u64, bool>(parent, key);
    asserts(!present || typed);
    let value = if (typed) *df::borrow<u64, bool>(parent, key) else false;
    let result = raw_fields::remove_bool_opt(parent, key);
    ensures(result.is_some() == present);
    ensures(!df::exists(parent, key));
    if (present) ensures(*result.borrow() == value);
    result
}
