
// Proof-only access to the private framework constructor. No storage operations
// here (object cannot depend on dynamic_field), and no executable public API.
#[mode(spec), ext(spec_only)]
public fun spec_uid_from_hash(address: address): UID {
    new_uid_from_hash(address)
}
