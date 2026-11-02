/// Native Sui multisig authentication; no off-chain service or caller-labelled address.
module sui_edge::multisig_admin {
    use std::bcs;
    use sui::clock::Clock;
    use sui::hash;
    use sui_edge::customer::{Self, Customer};
    use sui_edge::security_policy::{Self, Action};

    const EConfiguration: u64 = 0;
    const EAuthority: u64 = 1;
    const EAction: u64 = 2;

    /// Ordered Sui public keys include their scheme flag (0/1/2/6). The
    /// derivation is flag 3 || threshold LE u16 || (flagged key || weight)*.
    /// Native transaction authentication verifies the threshold before Move runs.
    public fun administrator(keys: &vector<vector<u8>>, weights: &vector<u8>, threshold: u16): address {
        assert!(keys.length() > 0 && keys.length() <= 10 && keys.length() == weights.length(), EConfiguration);
        let mut bytes = vector[3];
        bytes.append(bcs::to_bytes(&threshold));
        let mut total = 0u64;
        let mut i = 0;
        while (i < keys.length()) {
            let key = &keys[i];
            assert!(!key.is_empty(), EConfiguration);
            let flag = key[0];
            assert!((flag == 0 && key.length() == 33) || ((flag == 1 || flag == 2 || flag == 6) && key.length() == 34), EConfiguration);
            let mut j = 0;
            while (j < i) { assert!(keys[j] != *key, EConfiguration); j = j + 1; };
            assert!(weights[i] > 0, EConfiguration);
            total = total + (weights[i] as u64);
            bytes.append(*key); bytes.push_back(weights[i]); i = i + 1;
        };
        assert!(threshold > 0 && (threshold as u64) <= total, EConfiguration);
        sui::address::from_bytes(hash::blake2b256(&bytes))
    }

    /// First add the derived address as an administrator using current policy.
    /// Enrollment then requires a real transaction from its native quorum.
    /// Reconfiguration uses the current factors, including any other mechanisms.
    public fun enroll(customer: &mut Customer, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16, ctx: &mut TxContext) {
        customer::assert_security_administrator(customer, ctx);
        let sender = administrator(&keys, &weights, threshold);
        assert!(ctx.sender() == sender, EAuthority);
        customer::configure_security_factor(customer, 1, binding(sender, keys, weights, threshold), ctx);
    }

    /// Convenience producer for the exact existing policy-change wrapper.
    public fun prepare_policy_change(customer: &Customer, requirements: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&std::string::utf8(b"set_security")), bcs::to_bytes(&object::id(customer)), bcs::to_bytes(&requirements)];
        customer::prepare_security_action(customer, security_policy::action_intent(object::id(customer), bcs::to_bytes(&arguments)), expires_ms, clock, ctx);
    }

    public fun enroll_secured(customer: &mut Customer, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16, action: Action, clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&std::string::utf8(b"enroll_multisig")), bcs::to_bytes(customer), bcs::to_bytes(&keys), bcs::to_bytes(&weights), bcs::to_bytes(&threshold)];
        customer::begin_security_action(customer, action, arguments, 7, false, clock, ctx);
        enroll(customer, keys, weights, threshold, ctx);
        customer::finish_security_action(customer);
    }

    /// Attest the exact immutable action, not a browser approval flag. Revoked
    /// administrators, replaced configurations and stale generations cannot attest.
    public fun approve(customer: &Customer, action: &mut Action, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16, clock: &Clock, ctx: &mut TxContext) {
        customer::assert_security_administrator(customer, ctx);
        let sender = administrator(&keys, &weights, threshold);
        assert!(ctx.sender() == sender, EAuthority);
        assert!(customer::security_factor_configuration(customer, 1) == binding(sender, keys, weights, threshold), EAuthority);
        assert!(security_policy::customer_id(action) == object::id(customer)
            && security_policy::action_generation(action) == security_policy::generation(&customer::security(customer))
            && clock.timestamp_ms() < security_policy::expires_ms(action), EAction);
        security_policy::attest(action, 1);
    }
    fun binding(sender: address, keys: vector<vector<u8>>, weights: vector<u8>, threshold: u16): vector<u8> {
        let mut bytes = bcs::to_bytes(&sender);
        bytes.append(bcs::to_bytes(&keys)); bytes.append(bcs::to_bytes(&weights));
        bytes.append(bcs::to_bytes(&threshold)); bytes
    }
}
