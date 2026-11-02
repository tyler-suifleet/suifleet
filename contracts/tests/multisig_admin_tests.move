#[test_only]
module sui_edge::multisig_admin_tests {
    use std::bcs;
    use std::string::utf8;
    use sui::clock::{Self, Clock};
    use sui::test_scenario::{Self as ts};
    use sui_edge::customer::{Self, Customer};
    use sui_edge::device_registry::{Self, DeviceRegistry};
    use sui_edge::security_policy::{Self as policy, Action};
    use sui_edge::multisig_admin as multisig;
    const QUORUM: address = @0xc89be3da99a6d04b32f1d18490210046412411d185a4817d62bb49d60f3d4d9d;
    fun keys(): vector<vector<u8>> { vector[
        x"008a88e3dd7409f195fd52db2d3cba5d72ca6709bf1d94121bf3748801b40f6f5c",
        x"008139770ea87d175f56a35466c34c7ecccb8d8a91b4ee37a25df60f5b8fc9b394",
        x"00ed4928c628d1c2c6eae90338905995612959273a5c63f93636c14614ac8737d1"
    ] }
    fun setup(): ts::Scenario {
        let mut s = ts::begin(@0xA);
        device_registry::init_for_testing(s.ctx());
        clock::create_for_testing(s.ctx()).share_for_testing(); s.next_tx(@0xA);
        let mut r = s.take_shared<DeviceRegistry>();
        customer::create(&mut r, utf8(b"multisig"), s.ctx()); ts::return_shared(r); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); customer::initialize_security(&mut c, s.ctx());
        customer::set_credential(&mut c, QUORUM, true, true, s.ctx()); ts::return_shared(c); s.next_tx(QUORUM);
        let mut c = s.take_shared<Customer>(); multisig::enroll(&mut c, keys(), vector[1,1,1], 2, s.ctx());
        customer::set_security(&mut c, vector[1,1,1,1,1,1,1], s.ctx()); ts::return_shared(c); s.next_tx(QUORUM); s
    }
    fun prepare(s: &mut ts::Scenario, requirements: vector<u8>) {
        let c = s.take_shared<Customer>(); let clock = s.take_shared<Clock>();
        let args = vector[bcs::to_bytes(&utf8(b"set_security")), bcs::to_bytes(&c), bcs::to_bytes(&requirements)];
        let intent = policy::action_intent(object::id(&c), bcs::to_bytes(&args));
        customer::prepare_security_action(&c, intent, 1000, &clock, s.ctx()); ts::return_shared(c); ts::return_shared(clock); s.next_tx(QUORUM);
    }
    #[test] fun sdk_address_vector() { assert!(multisig::administrator(&keys(), &vector[1,1,1], 2) == QUORUM); }
    #[test, expected_failure(abort_code=0, location=sui_edge::multisig_admin)]
    fun duplicate_key() { let k = keys()[0]; multisig::administrator(&vector[k,k], &vector[1,1], 2); }
    #[test, expected_failure(abort_code=0, location=sui_edge::multisig_admin)]
    fun impossible_threshold() { multisig::administrator(&keys(), &vector[1,1,1], 4); }
    #[test, expected_failure(abort_code=0, location=sui_edge::multisig_admin)]
    fun zero_weight() { multisig::administrator(&keys(), &vector[1,0,1], 2); }
    #[test, expected_failure(abort_code=0, location=sui_edge::multisig_admin)]
    fun unsupported_key() { multisig::administrator(&vector[x"03"], &vector[1], 1); }
    #[test, expected_failure(abort_code=1, location=sui_edge::security_policy)]
    fun ordinary_admin_cannot_disable() {
        let mut s = setup(); s.next_tx(@0xA); let mut c = s.take_shared<Customer>();
        customer::set_security(&mut c, vector[0,0,0,0,0,0,0], s.ctx()); ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code=1, location=sui_edge::multisig_admin)]
    fun ordinary_admin_cannot_approve() {
        let mut s = setup(); prepare(&mut s, vector[0,0,0,0,0,0,0]); s.next_tx(@0xA);
        let c = s.take_shared<Customer>(); let mut a = s.take_shared<Action>(); let clock = s.take_shared<Clock>();
        multisig::approve(&c, &mut a, keys(), vector[1,1,1], 2, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(a); ts::return_shared(clock); s.end();
    }
    #[test] fun quorum_authorizes_exact_recovery() {
        let mut s = setup(); let off = vector[0,0,0,0,0,0,0]; prepare(&mut s, off);
        let mut c = s.take_shared<Customer>(); let mut a = s.take_shared<Action>(); let clock = s.take_shared<Clock>();
        multisig::approve(&c, &mut a, keys(), vector[1,1,1], 2, &clock, s.ctx());
        customer::set_security_secured(&mut c, off, a, &clock, s.ctx());
        assert!(policy::requirements(&customer::security(&c)) == off);
        ts::return_shared(c); ts::return_shared(clock); s.next_tx(@0xA);
        let mut c = s.take_shared<Customer>(); customer::set_credential(&mut c, @0xB, true, true, s.ctx()); ts::return_shared(c); s.end();
    }
    #[test, expected_failure(abort_code=0, location=sui_edge::security_policy)]
    fun quorum_cannot_substitute_action() {
        let mut s = setup(); prepare(&mut s, vector[0,0,0,0,0,0,0]);
        let mut c = s.take_shared<Customer>(); let mut a = s.take_shared<Action>(); let clock = s.take_shared<Clock>();
        multisig::approve(&c, &mut a, keys(), vector[1,1,1], 2, &clock, s.ctx());
        customer::set_security_secured(&mut c, vector[1,0,0,0,0,0,0], a, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(clock); s.end();
    }
    #[test, expected_failure(abort_code=1, location=sui_edge::multisig_admin)]
    fun configuration_substitution() {
        let mut s = setup(); prepare(&mut s, vector[0,0,0,0,0,0,0]);
        let c = s.take_shared<Customer>(); let mut a = s.take_shared<Action>(); let clock = s.take_shared<Clock>();
        multisig::approve(&c, &mut a, keys(), vector[1,1,1], 1, &clock, s.ctx());
        ts::return_shared(c); ts::return_shared(a); ts::return_shared(clock); s.end();
    }
}
