/// Executable conformance fixtures: compiled by the pinned release VM and prover.
module sf050_native::raw_fields;

use sui::dynamic_field as df;

public fun fresh_roundtrip(ctx: &mut TxContext) {
    let mut parent = object::new(ctx);
    assert!(!df::exists(&parent, 7u64), 100);
    df::add(&mut parent, 99u64, 123u64);
    df::add(&mut parent, 7u8, true);
    df::add(&mut parent, 7u64, 42u64);
    assert!(df::exists(&parent, 7u64), 101);
    assert!(df::exists_with_type<u64, u64>(&parent, 7), 102);
    assert!(!df::exists_with_type<u64, bool>(&parent, 7), 103);
    assert!(*df::borrow<u64, u64>(&parent, 7) == 42, 104);
    *df::borrow_mut<u64, u64>(&mut parent, 7) = 43;
    assert!(df::exists(&parent, 7u64), 105);
    assert!(df::remove<u64, u64>(&mut parent, 7) == 43, 106);
    assert!(!df::exists(&parent, 7u64), 107);
    assert!(!df::exists_with_type<u64, u64>(&parent, 7), 108);
    df::add(&mut parent, 7u64, true);
    assert!(df::exists_with_type<u64, bool>(&parent, 7), 109);
    assert!(!df::exists_with_type<u64, u64>(&parent, 7), 110);
    assert!(df::remove<u64, bool>(&mut parent, 7), 111);
    assert!(!df::exists(&parent, 7u64), 112);
    // Same parent, untouched key and distinct nominal Name type remain framed.
    assert!(df::remove<u64, u64>(&mut parent, 99) == 123, 113);
    assert!(df::exists_with_type<u8, bool>(&parent, 7), 114);
    assert!(df::remove<u8, bool>(&mut parent, 7), 115);
    parent.delete();
}

public fun add_to_arbitrary(parent: &mut UID, key: u64, value: u64) {
    df::add(parent, key, value);
}

public fun remove_arbitrary(parent: &mut UID, key: u64): u64 {
    df::remove<u64, u64>(parent, key)
}

public fun unknown_occupancy(parent: &UID, key: u64): bool {
    // A third, unmodeled Value type may occupy this name.
    df::exists(parent, key) && !df::exists_with_type<u64, u64>(parent, key)
        && !df::exists_with_type<u64, bool>(parent, key)
}

public fun foreign_parent(ctx: &mut TxContext) {
    let mut p = object::new(ctx);
    let mut q = object::new(ctx);
    df::add(&mut p, 7u64, 42u64);
    df::add(&mut q, 7u64, true);
    let _ = df::remove<u64, u64>(&mut p, 7);
    assert!(!df::exists(&p, 7u64), 120);
    assert!(df::exists_with_type<u64, bool>(&q, 7), 121);
    assert!(df::remove<u64, bool>(&mut q, 7), 122);
    p.delete(); q.delete();
}

public struct NameOnly has copy, drop, store { nonce: u64 }
public fun name_only_key(nonce: u64): NameOnly { NameOnly { nonce } }
public fun name_only_occupied(parent: &UID, nonce: u64): bool {
    df::exists(parent, name_only_key(nonce))
}

public fun borrow_bool(parent: &UID, key: u64): bool {
    *df::borrow<u64, bool>(parent, key)
}
public fun remove_bool_opt(parent: &mut UID, key: u64): Option<bool> {
    df::remove_opt<u64, bool>(parent, key)
}
