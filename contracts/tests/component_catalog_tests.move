#[test_only]
module sui_edge::component_catalog_tests {
    use std::string;
    use sui::test_scenario as ts;
    use sui::clock;
    use sui_edge::component_catalog::{Self as catalog, ComponentCatalog, ComponentVersion};
    use sui_edge::deployment_manager::DeploymentRecord;

    fun publish(s: &mut ts::Scenario, version: vector<u8>) {
        let mut c = ts::take_shared<ComponentCatalog>(s);
        let clock = clock::create_for_testing(ts::ctx(s));
        catalog::publish_version(&mut c, string::utf8(b"app"), string::utf8(version),
            vector[string::utf8(b"linux"), string::utf8(b"linux")],
            vector[string::utf8(b"x86_64"), string::utf8(b"aarch64")],
            vector[string::utf8(b"recipe_x86"), string::utf8(b"recipe_arm")],
            vector[string::utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),string::utf8(b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")],
            vector[string::utf8(b"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"),string::utf8(b"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd")],
            &clock, ts::ctx(s));
        clock::destroy_for_testing(clock); ts::return_shared(c);
    }
    #[test]
    fun immutable_registration_discovery_deployment_cleanup() {
        let mut s = ts::begin(@0xAD);
        catalog::create_catalog(ts::ctx(&mut s));
        ts::next_tx(&mut s, @0xAD); publish(&mut s, b"1.2.3-rc.1+build.5");
        ts::next_tx(&mut s, @0xAD);
        let c = ts::take_shared<ComponentCatalog>(&s);
        assert!(catalog::versions(&c).size() == 1);
        assert!(catalog::publisher(&c) == @0xAD); ts::return_shared(c);
        let v = ts::take_immutable<ComponentVersion>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        catalog::create_deployment(&v, vector[], vector[object::id_from_address(@0xD)], &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock); ts::return_immutable(v);
        ts::next_tx(&mut s, @0xAD);
        let d = ts::take_shared<DeploymentRecord>(&s);
        catalog::delete_deployment(d, ts::ctx(&mut s));
        ts::end(s);
    }
    #[test, expected_failure(abort_code = 0, location = sui_edge::component_catalog)]
    fun unauthorized_registration() {
        let mut s = ts::begin(@0xAD); catalog::create_catalog(ts::ctx(&mut s));
        ts::next_tx(&mut s, @0xBAD); publish(&mut s, b"1.0.0"); ts::end(s);
    }
    #[test, expected_failure(abort_code = 4, location = sui_edge::component_catalog)]
    fun duplicate_registration() {
        let mut s = ts::begin(@0xAD); catalog::create_catalog(ts::ctx(&mut s));
        ts::next_tx(&mut s, @0xAD); publish(&mut s, b"1.0.0");
        ts::next_tx(&mut s, @0xAD); publish(&mut s, b"1.0.0"); ts::end(s);
    }
    #[test]
    fun valid_semver() {
        catalog::validate_version(&string::utf8(b"0.0.0"));
        catalog::validate_version(&string::utf8(b"18446744073709551615.0.1-alpha.0+001"));
    }
    #[test, expected_failure(abort_code = 2, location = sui_edge::component_catalog)]
    fun invalid_semver() { catalog::validate_version(&string::utf8(b"1.0.0-01")); }
    #[test, expected_failure(abort_code = 2, location = sui_edge::component_catalog)]
    fun overflow_semver() { catalog::validate_version(&string::utf8(b"18446744073709551616.0.0")); }
    #[test, expected_failure(abort_code = 3, location = sui_edge::component_catalog)]
    fun invalid_hash() { catalog::validate_hash(&string::utf8(b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")); }
    fun invalid_manifest(mode: u8) {
        let mut s = ts::begin(@0xAD); catalog::create_catalog(ts::ctx(&mut s));
        ts::next_tx(&mut s, @0xAD);
        let mut c = ts::take_shared<ComponentCatalog>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let os = if (mode == 0) vector[] else vector[string::utf8(b"linux"), string::utf8(b"linux")];
        let arch = if (mode == 1) vector[string::utf8(b"x86_64")] else if (mode == 2)
            vector[string::utf8(b"x86_64"), string::utf8(b"x86_64")] else
            vector[string::utf8(b"x86_64"), string::utf8(b"armv7")];
        catalog::publish_version(&mut c, string::utf8(b"app"), string::utf8(b"1.0.0"), os, arch,
            vector[string::utf8(b"one"),string::utf8(b"two")],
            vector[string::utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),string::utf8(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")],
            vector[string::utf8(b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),string::utf8(b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")], &clock, ts::ctx(&mut s));
        clock::destroy_for_testing(clock); ts::return_shared(c); ts::end(s);
    }
    #[test, expected_failure(abort_code = 3, location = sui_edge::component_catalog)]
    fun empty_manifests() { invalid_manifest(0); }
    #[test, expected_failure(abort_code = 3, location = sui_edge::component_catalog)]
    fun mismatched_vectors() { invalid_manifest(1); }
    #[test, expected_failure(abort_code = 4, location = sui_edge::component_catalog)]
    fun duplicate_platforms() { invalid_manifest(2); }
    #[test, expected_failure(abort_code = 3, location = sui_edge::component_catalog)]
    fun unsupported_platform() { invalid_manifest(3); }

}
