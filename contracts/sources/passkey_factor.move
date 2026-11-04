/// Native Sui passkey authentication is verified by validators. The passkey
/// sponsors the exact wallet transaction; both signatures are mandatory.
module sui_edge::passkey_factor {
    const SMALL_0: u64 = 0;
    const SMALL_2: u64 = 2;
    const SMALL_1: u64 = 1;
    const SMALL_33: u64 = 33;
    const SMALL_64: u64 = 64;
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
        g0(public_key.length() == SMALL_33); g0((public_key[SMALL_0] == 2 || public_key[SMALL_0] == 3));
        let mut bytes = vector[6];
        bytes.append(public_key);
        address::from_bytes(hash::blake2b256(&bytes))
    }

    /// Transaction sender must authenticate under Sui's 0x06 passkey scheme.
    /// Its address is independently derived from the compressed P-256 key.
    public fun prove_enrollment(customer: &Customer, administrator: address,
        public_key: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        customer::k2(customer, administrator);
        let key = key_address(public_key);
        g0(ctx.sender() == key); g0(key != administrator);
        g1(expires_ms > clock.timestamp_ms()); g1(expires_ms - clock.timestamp_ms() <= 600000);
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
        g1(reader.into_remainder_bytes().is_empty()); g1(keys.length() % SMALL_2 == 0);
        keys
    }


    public fun enroll(customer: &mut Customer, proof: Enrollment, clock: &Clock, ctx: &mut TxContext) {
        let Enrollment { id, customer: owner, administrator, key, generation, expires_ms } = proof;
        g1(owner == object::id(customer)); g1(generation == policy::generation(&customer::security(customer))); g1(clock.timestamp_ms() < expires_ms);
        customer::k2(customer, administrator);
        let mut entries = keys(customer);
        let mut i = SMALL_0;
        while (i < entries.length()) {
            g0(entries[i + 1] != key);
            i = i + SMALL_2;
        };
        g0(entries.length() < SMALL_64);
        entries.push_back(administrator); entries.push_back(key);
        customer::k6(customer, FACTOR, bcs::to_bytes(&entries), ctx);
        id.delete();
    }
    public fun remove(customer: &mut Customer, administrator: address, key: address, ctx: &mut TxContext) {
        customer::assert_admin(customer, ctx);
        let mut entries = keys(customer);
        let mut found = false;
        let mut i = SMALL_0;
        while (i < entries.length()) {
            if (entries[i] == administrator && entries[i + 1] == key) {
                entries.remove(i); entries.remove(i); found = true;
            } else i = i + SMALL_2;
        };
        g0(found);
        if (entries.is_empty()) {
            let requirements = policy::requirements(&customer::security(customer));
            let mut j = SMALL_0;
            while (j < requirements.length()) { assert!((requirements[j] & FACTOR) == 0, ERecovery); j = j + SMALL_1; };
        };
        customer::k6(customer, FACTOR, if (entries.is_empty()) vector[] else bcs::to_bytes(&entries), ctx);
    }

    /// Shared EKey guard; preserve evaluation order and abort code.
    fun g0(condition: bool) { if (!condition) abort EKey }
    /// Shared EBinding guard; preserve evaluation order and abort code.
    fun g1(condition: bool) { if (!condition) abort EBinding }
}
