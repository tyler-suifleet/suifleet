
// Proof-only exact observation: unlike exists(), this also sees wrong-typed slots.
#[mode(spec), ext(spec_only)]
public fun spec_name_exists<Name: copy + drop + store>(object: &UID, name: Name): bool {
    field::exists(object, Wrapper { name })
}
