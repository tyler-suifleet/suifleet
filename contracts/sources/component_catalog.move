/// Immutable authenticated component recipes; manifest order defines precedence.
module sui_edge::component_catalog {
    use std::string::{Self, String};
    use sui::clock::Clock;
    use sui::event;
    use sui::vec_map::{Self, VecMap};
    use sui_edge::deployment_manager::{Self, DeploymentRecord};

    const EPublisher: u64 = 0;
    const EIdentity: u64 = 1;
    const EVersion: u64 = 2;
    const EManifest: u64 = 3;
    const EDuplicate: u64 = 4;

    public struct ComponentCatalog has key {
        id: UID,
        publisher: address,
        versions: VecMap<String, ID>,
    }
    public struct ComponentVersion has key {
        id: UID,
        catalog_id: ID,
        publisher: address,
        component_name: String,
        version: String,
        schema_version: u64,
        created_at: u64,
        platform_os: vector<String>,
        platform_arch: vector<String>,
        recipe_blob_ids: vector<String>,
        recipe_sha256s: vector<String>,
        artifact_sha256s: vector<String>,
    }
    public struct CatalogCreated has copy, drop { catalog_id: ID, publisher: address }
    public struct VersionPublished has copy, drop {
        catalog_id: ID, version_id: ID, publisher: address, component_name: String, version: String,
    }
    public struct CatalogBindingKey has copy, drop, store {}
    public struct CatalogBinding has store, drop { version_id: ID }

    public fun create_catalog(ctx: &mut TxContext) {
        let catalog = ComponentCatalog { id: object::new(ctx), publisher: ctx.sender(), versions: vec_map::empty() };
        event::emit(CatalogCreated { catalog_id: object::id(&catalog), publisher: ctx.sender() });
        transfer::share_object(catalog);
    }

    public fun publish_version(
        catalog: &mut ComponentCatalog, component_name: String, version: String,
        platform_os: vector<String>, platform_arch: vector<String>, recipe_blob_ids: vector<String>,
        recipe_sha256s: vector<String>, artifact_sha256s: vector<String>, clock: &Clock, ctx: &mut TxContext,
    ) {
        assert!(ctx.sender() == catalog.publisher, EPublisher);
        validate_name(&component_name);
        validate_version(&version);
        let n = platform_os.length();
        assert!(n > 0 && n <= 16 && platform_arch.length() == n && recipe_blob_ids.length() == n
            && recipe_sha256s.length() == n && artifact_sha256s.length() == n, EManifest);
        let mut i = 0;
        while (i < n) {
            assert!(valid_os(&platform_os[i]) && valid_arch(&platform_arch[i]), EManifest);
            validate_token(&recipe_blob_ids[i], 128);
            validate_hash(&recipe_sha256s[i]); validate_hash(&artifact_sha256s[i]);
            let mut j = 0;
            while (j < i) {
                assert!(platform_os[i] != platform_os[j] || platform_arch[i] != platform_arch[j], EDuplicate);
                j = j + 1;
            };
            i = i + 1;
        };
        let mut key = component_name;
        key.append(string::utf8(b"@")); key.append(version);
        assert!(!catalog.versions.contains(&key), EDuplicate);
        let value = ComponentVersion { id: object::new(ctx), catalog_id: object::id(catalog),
            publisher: ctx.sender(), component_name, version, schema_version: 1, created_at: clock.timestamp_ms(),
            platform_os, platform_arch, recipe_blob_ids, recipe_sha256s, artifact_sha256s };
        catalog.versions.insert(key, object::id(&value));
        event::emit(VersionPublished { catalog_id: object::id(catalog), version_id: object::id(&value),
            publisher: ctx.sender(), component_name, version });
        transfer::freeze_object(value);
    }

    public fun create_deployment(value: &ComponentVersion, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        deployment_manager::create_catalog_deployment(value.component_name, value.version, groups, devices,
            CatalogBindingKey {}, CatalogBinding { version_id: object::id(value) }, clock, ctx);
    }
    /// Publish with admission paused atomically, so operators can configure a
    /// schedule before any device is eligible to start.
    public fun create_deployment_paused(value: &ComponentVersion, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        let mut record = deployment_manager::new_catalog_deployment(value.component_name, value.version, groups, devices,
            CatalogBindingKey {}, CatalogBinding { version_id: object::id(value) }, clock, ctx);
        deployment_manager::set_control(&mut record, 1, 0, 0, 0, 0, 0, ctx);
        deployment_manager::share_deployment(record);
    }
    public fun delete_deployment(mut deployment: DeploymentRecord, ctx: &TxContext) {
        deployment_manager::remove_catalog_binding<CatalogBindingKey, CatalogBinding>(&mut deployment, CatalogBindingKey {}, ctx);
        deployment_manager::delete_deployment(deployment, ctx);
    }
    public fun versions(catalog: &ComponentCatalog): &VecMap<String, ID> { &catalog.versions }
    public fun publisher(catalog: &ComponentCatalog): address { catalog.publisher }

    fun alpha(b: u8): bool { (b >= 65 && b <= 90) || (b >= 97 && b <= 122) }
    fun digit(b: u8): bool { b >= 48 && b <= 57 }
    fun validate_token(s: &String, max: u64) {
        let b = s.as_bytes(); assert!(b.length() > 0 && b.length() <= max, EManifest);
        b.do_ref!(|c| assert!(alpha(*c) || digit(*c) || *c == 45 || *c == 95, EManifest));
    }
    public fun validate_name(s: &String) {
        let b = s.as_bytes(); assert!(b.length() > 0 && b.length() <= 128, EIdentity);
        b.do_ref!(|c| assert!(alpha(*c) || digit(*c) || *c == 45 || *c == 95 || *c == 46, EIdentity));
    }
    public fun validate_hash(s: &String) {
        let b = s.as_bytes(); assert!(b.length() == 64, EManifest);
        b.do_ref!(|c| assert!(digit(*c) || (*c >= 97 && *c <= 102), EManifest));
    }
    fun valid_os(s: &String): bool {
        *s == string::utf8(b"linux") || *s == string::utf8(b"macos") || *s == string::utf8(b"*")
    }
    /// Rust's `std::env::consts::ARCH` names; `x86` is 32-bit i686 (SF-076).
    fun valid_arch(s: &String): bool {
        *s == string::utf8(b"x86_64") || *s == string::utf8(b"aarch64") || *s == string::utf8(b"arm")
            || *s == string::utf8(b"x86") || *s == string::utf8(b"*")
    }
    /// SemVer 2.0 with bounded u64 core numbers, <=128 ASCII bytes.
    public fun validate_version(s: &String) {
        let b = s.as_bytes(); let n = b.length(); assert!(n > 0 && n <= 128, EVersion);
        let mut i = 0; let mut part = 0;
        while (part < 3) {
            let start = i; let mut number: u64 = 0;
            while (i < n && digit(b[i])) {
                let d = (b[i] - 48) as u64;
                assert!(number <= (18446744073709551615 - d) / 10, EVersion);
                number = number * 10 + d; i = i + 1;
            };
            assert!(i > start && (i - start == 1 || b[start] != 48), EVersion);
            part = part + 1;
            if (part < 3) { assert!(i < n && b[i] == 46, EVersion); i = i + 1; };
        };
        if (i == n) return;
        let mut prerelease = b[i] == 45;
        assert!(prerelease || b[i] == 43, EVersion); i = i + 1;
        loop {
            let start = i; let mut numeric = true;
            while (i < n && b[i] != 46 && b[i] != 43) {
                assert!(alpha(b[i]) || digit(b[i]) || b[i] == 45, EVersion);
                if (!digit(b[i])) numeric = false;
                i = i + 1;
            };
            assert!(i > start, EVersion);
            if (prerelease && numeric) assert!(i - start == 1 || b[start] != 48, EVersion);
            if (i == n) break;
            if (b[i] == 43) { assert!(prerelease, EVersion); prerelease = false; };
            i = i + 1;
        };
    }
}
