/// Native Sui passkey authentication is verified by validators. The passkey
/// sponsors the exact wallet transaction; both signatures are mandatory.
module sui_edge::passkey_factor {
    use sui::address;
    use sui::bcs;
    use sui::clock::Clock;
    use sui::hash;
    use sui_edge::customer::{Self, Customer};
    use sui_edge::security_policy as policy;

    const EKey: u64 = 0;
    const EBinding: u64 = 1;
    const ERecovery: u64 = 2;
    const FACTOR: u8 = 2;

    /// Possession proof cannot be transferred or constructed by old packages.
    public struct ProofCreated has copy, drop { proof_id: ID, customer: ID, administrator: address, key: address }
    public struct Enrollment has key {
        id: UID, customer: ID, administrator: address, key: address,
        generation: u64, expires_ms: u64,
    }

    public fun key_address(public_key: vector<u8>): address {
        assert!(public_key.length() == 33 && (public_key[0] == 2 || public_key[0] == 3), EKey);
        let mut bytes = vector[6];
        bytes.append(public_key);
        address::from_bytes(hash::blake2b256(&bytes))
    }

    /// Transaction sender must authenticate under Sui's 0x06 passkey scheme.
    /// Its address is independently derived from the compressed P-256 key.
    public fun prove_enrollment(customer: &Customer, administrator: address,
        public_key: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        customer::assert_security_administrator_address(customer, administrator);
        let key = key_address(public_key);
        assert!(ctx.sender() == key && key != administrator, EKey);
        assert!(expires_ms > clock.timestamp_ms() && expires_ms - clock.timestamp_ms() <= 600000, EBinding);
        let id = object::new(ctx);
        sui::event::emit(ProofCreated { proof_id: id.to_inner(), customer: object::id(customer), administrator, key });
        transfer::share_object(Enrollment { id, customer: object::id(customer),
            administrator, key, generation: policy::generation(&customer::security(customer)), expires_ms });
    }

    /// Binding schema: BCS vector<address>, alternating administrator/key pairs.
    public fun keys(customer: &Customer): vector<address> {
        let bytes = customer::security_factor_configuration(customer, FACTOR);
        if (bytes.is_empty()) return vector[];
        let mut reader = bcs::new(bytes);
        let keys = reader.peel_vec_address();
        assert!(reader.into_remainder_bytes().is_empty() && keys.length() % 2 == 0, EBinding);
        keys
    }

    fun install(customer: &mut Customer, proof: Enrollment, clock: &Clock, ctx: &mut TxContext) {
        let Enrollment { id, customer: owner, administrator, key, generation, expires_ms } = proof;
        assert!(owner == object::id(customer) && generation == policy::generation(&customer::security(customer))
            && clock.timestamp_ms() < expires_ms, EBinding);
        customer::assert_security_administrator_address(customer, administrator);
        let mut entries = keys(customer);
        let mut i = 0;
        while (i < entries.length()) {
            assert!(entries[i + 1] != key, EKey);
            i = i + 2;
        };
        assert!(entries.length() < 64, EKey);
        entries.push_back(administrator); entries.push_back(key);
        customer::configure_security_factor(customer, FACTOR, bcs::to_bytes(&entries), ctx);
        id.delete();
    }

    public fun enroll(customer: &mut Customer, proof: Enrollment, clock: &Clock, ctx: &mut TxContext) {
        install(customer, proof, clock, ctx);
    }
    fun remove_key(customer: &mut Customer, administrator: address, key: address, ctx: &mut TxContext) {
        customer::assert_admin(customer, ctx);
        let mut entries = keys(customer);
        let mut found = false;
        let mut i = 0;
        while (i < entries.length()) {
            if (entries[i] == administrator && entries[i + 1] == key) {
                entries.remove(i); entries.remove(i); found = true;
            } else i = i + 2;
        };
        assert!(found, EKey);
        if (entries.is_empty()) {
            let requirements = policy::requirements(&customer::security(customer));
            let mut j = 0;
            while (j < requirements.length()) { assert!((requirements[j] & FACTOR) == 0, ERecovery); j = j + 1; };
        };
        customer::configure_security_factor(customer, FACTOR, if (entries.is_empty()) vector[] else bcs::to_bytes(&entries), ctx);
    }
    public fun remove(customer: &mut Customer, administrator: address, key: address, ctx: &mut TxContext) {
        remove_key(customer, administrator, key, ctx);
    }
}
