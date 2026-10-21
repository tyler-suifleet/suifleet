/// Test-only stand-in for Seal's key server objects on a disposable localnet:
/// the same `KeyServer` layout with a versioned `KeyServerV1` dynamic field
/// (u64 key 1) holding the name, URL, key type and BLS12-381 G2 public key
/// that clients read and verify. Never published to a shared network.
module seal_test_key_server::key_server {
    use std::string::String;
    use sui::dynamic_field as df;

    public struct KeyServer has key {
        id: UID,
        first_version: u64,
        last_version: u64,
    }

    public struct KeyServerV1 has store, drop {
        name: String,
        url: String,
        key_type: u8,
        pk: vector<u8>,
    }

    entry fun register(name: String, url: String, pk: vector<u8>, ctx: &mut TxContext) {
        let mut server = KeyServer { id: object::new(ctx), first_version: 1, last_version: 1 };
        df::add(&mut server.id, 1u64, KeyServerV1 { name, url, key_type: 0, pk });
        transfer::transfer(server, ctx.sender());
    }
}
