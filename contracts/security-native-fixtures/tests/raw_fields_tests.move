#[test_only]
module sf050_native::raw_fields_tests;
use sf050_native::raw_fields;
use sui::dynamic_field as df;

#[test] fun roundtrip() { raw_fields::fresh_roundtrip(&mut tx_context::dummy()); }
#[test] fun parent_isolation() { raw_fields::foreign_parent(&mut tx_context::dummy()); }

#[test, expected_failure(abort_code = 0, location = sui::dynamic_field)]
fun duplicate_same_type() {
    let mut p = object::new(&mut tx_context::dummy());
    df::add(&mut p, 7u64, 1u64); df::add(&mut p, 7u64, 2u64);
    let _ = df::remove<u64, u64>(&mut p, 7); p.delete();
}
#[test, expected_failure(abort_code = 0, location = sui::dynamic_field)]
fun duplicate_other_type() {
    let mut p = object::new(&mut tx_context::dummy());
    df::add(&mut p, 7u64, 1u64); df::add(&mut p, 7u64, true);
    let _ = df::remove<u64, u64>(&mut p, 7); p.delete();
}
#[test, expected_failure(abort_code = 2, location = sui::dynamic_field)]
fun wrong_borrow() {
    let mut p = object::new(&mut tx_context::dummy());
    df::add(&mut p, 7u64, 1u64); let _ = raw_fields::borrow_bool(&p, 7);
    let _ = df::remove<u64, u64>(&mut p, 7); p.delete();
}
#[test, expected_failure(abort_code = 2, location = sui::dynamic_field)]
fun wrong_remove() {
    let mut p = object::new(&mut tx_context::dummy());
    df::add(&mut p, 7u64, 1u64); let _ = df::remove<u64, bool>(&mut p, 7);
    let _ = df::remove<u64, u64>(&mut p, 7); p.delete();
}
#[test, expected_failure(abort_code = 2, location = sui::dynamic_field)]
fun wrong_remove_opt() {
    let mut p = object::new(&mut tx_context::dummy());
    df::add(&mut p, 7u64, 1u64); let _ = raw_fields::remove_bool_opt(&mut p, 7);
    let _ = df::remove<u64, u64>(&mut p, 7); p.delete();
}
#[test] fun unknown_type() {
    let mut p = object::new(&mut tx_context::dummy());
    df::add(&mut p, 7u64, vector[1u8]);
    assert!(raw_fields::unknown_occupancy(&p, 7), 0);
    let _ = df::remove<u64, vector<u8>>(&mut p, 7); p.delete();
}
#[test, expected_failure(abort_code = 0, location = sui::dynamic_field)]
fun unknown_type_duplicate() {
    let mut p = object::new(&mut tx_context::dummy());
    df::add(&mut p, 7u64, vector[1u8]); raw_fields::add_to_arbitrary(&mut p, 7, 1);
    let _ = df::remove<u64, vector<u8>>(&mut p, 7); p.delete();
}

#[test] fun existing_uid_rewrap() {
    sf050_native::carrier_fields::rewrap(&mut tx_context::dummy());
}

#[test] fun object_field_two_slots() {
    sf050_native::object_fields::roundtrip(&mut tx_context::dummy());
}

#[test] fun name_only_unknown_type() {
    let mut p = object::new(&mut tx_context::dummy());
    let key = raw_fields::name_only_key(9);
    df::add(&mut p, key, vector[2u8]);
    assert!(raw_fields::name_only_occupied(&p, 9), 0);
    let _ = df::remove<raw_fields::NameOnly, vector<u8>>(&mut p, key); p.delete();
}

#[test] fun same_name_raw_object_noninterference() {
    sf050_native::overlap_fields::same_name_noninterference(&mut tx_context::dummy());
}
#[test] fun same_name_different_value_noninterference() {
    sf050_native::overlap_fields::raw_value_noninterference(&mut tx_context::dummy());
}
#[test, expected_failure(abort_code = 2, location = sui::dynamic_field)]
fun object_wrong_requested_type() {
    sf050_native::object_fields::wrong_requested_child(&mut tx_context::dummy());
}
#[test, expected_failure(abort_code = 0, location = sui::dynamic_field)]
fun object_duplicate_different_type() {
    sf050_native::object_fields::duplicate_child_type(&mut tx_context::dummy());
}
#[test, expected_failure(abort_code = 1, location = sui::dynamic_field)]
fun object_missing() {
    sf050_native::object_fields::missing_child(&mut tx_context::dummy());
}
#[test] fun object_readd_mutation_frame() {
    sf050_native::object_fields::remove_readd_and_frame(&mut tx_context::dummy());
}
