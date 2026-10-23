
// Proof-only exact observation: unlike exists(), this also sees wrong-typed slots.
#[mode(spec), ext(spec_only)]
public fun spec_name_exists<Name: copy + drop + store>(object: &UID, name: Name): bool {
    field::exists(object, Wrapper { name })
}

// Exact observations expanded at the caller, preserving its parent carrier.
#[mode(spec), ext(spec_only)]
public fun spec_wrapper<Name: copy + drop + store>(name: Name): Wrapper<Name> {
    Wrapper { name }
}
#[mode(spec)]
public macro fun spec_name_exists_macro<$Name: copy + drop + store>($object: &UID, $name: $Name): bool {
    field::exists($object, spec_wrapper($name))
}
#[mode(spec)]
public macro fun spec_outer_typed_macro<$Name: copy + drop + store>($object: &UID, $name: $Name): bool {
    field::exists_with_type<Wrapper<$Name>, ID>($object, spec_wrapper($name))
}
#[mode(spec)]
public macro fun spec_outer_id_macro<$Name: copy + drop + store>($object: &UID, $name: $Name): ID {
    *field::borrow<Wrapper<$Name>, ID>($object, spec_wrapper($name))
}
