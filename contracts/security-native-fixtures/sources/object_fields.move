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
    add(&mut parent, 7, child);
    assert!(dof::exists(&parent.id, 7u64), 140);
    assert!(dof::exists_with_type<u64, Child>(&parent.id, 7), 141);
    assert!(object::id(dof::borrow<u64, Child>(&parent.id, 7)) == id, 142);
    let Child { id: child_id, payload } = dof::remove<u64, Child>(&mut parent.id, 7);
    assert!(payload == 42, 143); child_id.delete();
    assert!(!dof::exists(&parent.id, 7u64), 144);
    let Parent { id } = parent; id.delete();
}
