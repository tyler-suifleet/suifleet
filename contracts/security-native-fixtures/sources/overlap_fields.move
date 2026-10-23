module sf050_native::overlap_fields;
use sui::dynamic_object_field as dof;
public struct Parent has key { id: UID }
public struct Child has key, store { id: UID, payload: u64 }
/// Raw Name/Child and object Name/Child are independent physical namespaces.
/// Wrapper<Name> is private in the release API, which prevents raw Name from
/// addressing the object's outer field even when Name and Value are identical.
public fun same_name_noninterference(ctx: &mut TxContext) {
    let mut parent = Parent { id: object::new(ctx) };
    let raw_child = Child { id: object::new(ctx), payload: 11 };
    let object_child = Child { id: object::new(ctx), payload: 22 };
    let raw_id = object::id(&raw_child);
    let object_id = object::id(&object_child);
    sui::dynamic_field::add(&mut parent.id, 7u64, raw_child);
    dof::add(&mut parent.id, 7u64, object_child);
    assert!(object::id(sui::dynamic_field::borrow<u64, Child>(&parent.id, 7)) == raw_id, 150);
    assert!(object::id(dof::borrow<u64, Child>(&parent.id, 7)) == object_id, 151);
    dof::borrow_mut<u64, Child>(&mut parent.id, 7).payload = 33;
    assert!(sui::dynamic_field::borrow<u64, Child>(&parent.id, 7).payload == 11, 152);
    let Child { id, payload } = sui::dynamic_field::remove<u64, Child>(&mut parent.id, 7);
    assert!(payload == 11, 153); id.delete();
    assert!(dof::exists(&parent.id, 7u64), 154);
    let Child { id, payload } = dof::remove<u64, Child>(&mut parent.id, 7);
    assert!(payload == 33, 155); id.delete();
    let Parent { id } = parent; id.delete();
}

public fun raw_value_noninterference(ctx: &mut TxContext) {
    let mut parent = Parent { id: object::new(ctx) };
    sui::dynamic_field::add(&mut parent.id, 7u64, false);
    dof::add(&mut parent.id, 7u64, Child { id: object::new(ctx), payload: 22 });
    let Child { id, payload: _ } = dof::remove<u64, Child>(&mut parent.id, 7);
    id.delete();
    assert!(!*sui::dynamic_field::borrow<u64, bool>(&parent.id, 7), 156);
    let _ = sui::dynamic_field::remove<u64, bool>(&mut parent.id, 7);
    let Parent { id } = parent; id.delete();
}
