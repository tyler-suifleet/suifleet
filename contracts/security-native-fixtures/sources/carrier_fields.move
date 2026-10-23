/// Existing-UID rewrapping is legal Move and must preserve its native children.
module sf050_native::carrier_fields;
use sui::dynamic_field as df;
public struct Parent has store { id: UID }
public fun rewrap(ctx: &mut TxContext) {
    let mut p = Parent { id: object::new(ctx) };
    df::add(&mut p.id, 7u64, 42u64);
    let Parent { id } = p;
    let mut q = Parent { id };
    assert!(df::exists(&q.id, 7u64), 130);
    assert!(df::remove<u64, u64>(&mut q.id, 7) == 42, 131);
    let Parent { id } = q; id.delete();
}
