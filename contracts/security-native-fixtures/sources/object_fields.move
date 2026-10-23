/// Object-field fixtures retain parent provenance; Child contains its real UID.
module sf050_native::object_fields;
use sui::dynamic_object_field as dof;
public struct Parent has key { id: UID }
public struct Child has key, store { id: UID, payload: u64 }
public fun add(parent: &mut Parent, name: u64, child: Child) {
    dof::add(&mut parent.id, name, child);
}
public fun roundtrip(ctx: &mut TxContext) {
    let mut parent = Parent { id: object::new(ctx) };
    let child = Child { id: object::new(ctx), payload: 42 };
    let id = object::id(&child);
    dof::add(&mut parent.id, 7u64, child);
    assert!(dof::exists(&parent.id, 7u64), 140);
    assert!(dof::exists_with_type<u64, Child>(&parent.id, 7), 141);
    assert!(object::id(dof::borrow<u64, Child>(&parent.id, 7)) == id, 142);
    let Child { id: child_id, payload } = dof::remove<u64, Child>(&mut parent.id, 7);
    assert!(payload == 42, 143); child_id.delete();
    assert!(!dof::exists(&parent.id, 7u64), 144);
    let Parent { id } = parent; id.delete();
}

public struct OtherChild has key, store { id: UID }
public fun wrong_requested_child(ctx: &mut TxContext) {
    let mut p = Parent { id: object::new(ctx) };
    dof::add(&mut p.id, 7u64, Child { id: object::new(ctx), payload: 1 });
    let _ = dof::borrow<u64, OtherChild>(&p.id, 7);
    let Child { id, payload: _ } = dof::remove<u64, Child>(&mut p.id, 7); id.delete();
    let Parent { id } = p; id.delete();
}
public fun duplicate_child_type(ctx: &mut TxContext) {
    let mut p = Parent { id: object::new(ctx) };
    dof::add(&mut p.id, 7u64, Child { id: object::new(ctx), payload: 1 });
    dof::add(&mut p.id, 7u64, OtherChild { id: object::new(ctx) });
    let Child { id, payload: _ } = dof::remove<u64, Child>(&mut p.id, 7); id.delete();
    let Parent { id } = p; id.delete();
}
public fun missing_child(ctx: &mut TxContext) {
    let p = Parent { id: object::new(ctx) };
    let _ = dof::borrow<u64, Child>(&p.id, 7);
    let Parent { id } = p; id.delete();
}
public fun remove_readd_and_frame(ctx: &mut TxContext) {
    let mut p = Parent { id: object::new(ctx) };
    let mut other = Parent { id: object::new(ctx) };
    dof::add(&mut p.id, 7u64, Child { id: object::new(ctx), payload: 1 });
    dof::add(&mut p.id, 8u64, Child { id: object::new(ctx), payload: 2 });
    dof::add(&mut other.id, 7u64, Child { id: object::new(ctx), payload: 3 });
    let Child { id, payload: _ } = dof::remove<u64, Child>(&mut p.id, 7); id.delete();
    assert!(dof::exists_with_type<u64, Child>(&p.id, 8), 160);
    assert!(dof::exists_with_type<u64, Child>(&other.id, 7), 161);
    dof::add(&mut p.id, 7u64, Child { id: object::new(ctx), payload: 4 });
    dof::borrow_mut<u64, Child>(&mut p.id, 7).payload = 5;
    assert!(dof::borrow<u64, Child>(&p.id, 8).payload == 2, 162);
    assert!(dof::borrow<u64, Child>(&other.id, 7).payload == 3, 163);
    let Child { id, payload } = dof::remove<u64, Child>(&mut p.id, 7);
    assert!(payload == 5, 164); id.delete();
    let Child { id, payload: _ } = dof::remove<u64, Child>(&mut p.id, 8); id.delete();
    let Child { id, payload: _ } = dof::remove<u64, Child>(&mut other.id, 7); id.delete();
    let Parent { id } = p; id.delete();
    let Parent { id } = other; id.delete();
}

public fun allocation_distinct(ctx: &mut TxContext) {
    let first = sui::tx_context::fresh_object_address(ctx);
    let second = sui::tx_context::fresh_object_address(ctx);
    assert!(first != second, 176);
}

// No external input owns this child while it is stored. Removal must produce a
// local resource; the subsequent ordinary call must discharge VM separation.
public fun remove_and_readd(parent: &mut Parent, name: u64) {
    let child: Child = dof::remove(&mut parent.id, name);
    add(parent, name, child);
}
public fun take(parent: &mut Parent, name: u64): Child {
    dof::remove(&mut parent.id, name)
}
