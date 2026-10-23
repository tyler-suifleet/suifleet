module sf050_native::carrier_field_specs;
#[mode(spec), ext(spec(prove, target = sf050_native::carrier_fields::rewrap))]
fun raw_rewrap_spec(ctx: &mut TxContext) {
    sf050_native::carrier_fields::rewrap(ctx);
}

// This specifically exercises the framework's hashed UID constructor while a
// prior raw child remains. It must be rejected as unsupported heap preservation,
// never verified by initializing a second empty projection at the same address.
#[mode(spec), ext(spec(prove, target = hashed_child_reconstruction))]
fun hashed_child_reconstruction_spec(ctx: &mut TxContext) {
    hashed_child_reconstruction(ctx);
}

#[mode(spec), ext(spec_only)]
fun hashed_child_reconstruction(ctx: &mut TxContext) {
    let mut uid = sui::object::new(ctx);
    sui::dynamic_field::add(&mut uid, 7u64, 42u64);
    let address = uid.to_address();
    uid.delete();
    let mut reconstructed = sui::object::spec_uid_from_hash(address);
    assert!(sui::dynamic_field::exists(&reconstructed, 7u64), 170);
    let _ = sui::dynamic_field::remove<u64, u64>(&mut reconstructed, 7);
    reconstructed.delete();
}
