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
    public struct ShardedCatalogBindingKey has copy, drop, store {}
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
        publish_as(catalog, component_name, version, platform_os, platform_arch, recipe_blob_ids,
            recipe_sha256s, artifact_sha256s, clock, ctx.sender(), ctx);
    }
    /// Custody is an unforgeable customer object address, never a wallet label.
    public(package) fun adopt(catalog: &mut ComponentCatalog, authority: address, ctx: &TxContext) {
        assert!(catalog.publisher == ctx.sender(), EPublisher);
        catalog.publisher = authority;
    }
    public(package) fun publish_as(
        catalog: &mut ComponentCatalog, component_name: String, version: String,
        platform_os: vector<String>, platform_arch: vector<String>, recipe_blob_ids: vector<String>,
        recipe_sha256s: vector<String>, artifact_sha256s: vector<String>, clock: &Clock, authority: address, ctx: &mut TxContext,
    ) {
        assert!(catalog.publisher == authority, EPublisher);
        validate_name(&component_name);
        validate_version(&version);
        let n = platform_os.length();
        p5(n > 0 && n <= 16 && platform_arch.length() == n && recipe_blob_ids.length() == n
            && recipe_sha256s.length() == n && artifact_sha256s.length() == n);
        let mut i = 0;
        while (i < n) {
            p5(p3(&platform_os[i]) && p4(&platform_arch[i]));
            p2(&recipe_blob_ids[i], 128);
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
            publisher: authority, component_name, version, schema_version: 1, created_at: clock.timestamp_ms(),
            platform_os, platform_arch, recipe_blob_ids, recipe_sha256s, artifact_sha256s };
        catalog.versions.insert(key, object::id(&value));
        event::emit(VersionPublished { catalog_id: object::id(catalog), version_id: object::id(&value),
            publisher: authority, component_name, version });
        transfer::freeze_object(value);
    }

    /// Transaction-local catalog record, composable with desired-state revisions.
    public fun new_deployment(value: &ComponentVersion, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        deployment_manager::new_catalog_deployment(value.component_name, value.version, groups, devices,
            CatalogBindingKey {}, CatalogBinding { version_id: object::id(value) }, clock, ctx)
    }

    /// Bindings can only be installed by this module from a frozen version.
    public(package) fun has_authenticated_version(record: &DeploymentRecord): bool {
        deployment_manager::has_binding<CatalogBindingKey, CatalogBinding>(record, CatalogBindingKey {}) ||
            deployment_manager::has_binding<ShardedCatalogBindingKey, CatalogBinding>(record, ShardedCatalogBindingKey {})
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
    #[allow(lint(prefer_mut_tx_context))]
    public fun delete_deployment(mut deployment: DeploymentRecord, ctx: &TxContext) {
        if (deployment_manager::receipt_schema(&deployment) == 2 &&
            deployment_manager::receipts_version(&deployment) == 4) {
            deployment_manager::remove_catalog_binding<ShardedCatalogBindingKey, CatalogBinding>(&mut deployment, ShardedCatalogBindingKey {}, ctx);
        } else {
            deployment_manager::remove_catalog_binding<CatalogBindingKey, CatalogBinding>(&mut deployment, CatalogBindingKey {}, ctx);
        };
        deployment_manager::delete_deployment(deployment, ctx);
    }
    public fun begin_sharded_receipts(d: &mut DeploymentRecord, ctx: &mut TxContext) {
        deployment_manager::begin_catalog_receipts<CatalogBindingKey, ShardedCatalogBindingKey, CatalogBinding>(
            d, CatalogBindingKey {}, ShardedCatalogBindingKey {}, ctx);
    }
    public(package) fun remove_for_customer(record: &mut DeploymentRecord, authority: address) {
        if (deployment_manager::receipts_version(record) == 4) {
            deployment_manager::remove_catalog_binding_as<ShardedCatalogBindingKey, CatalogBinding>(record, ShardedCatalogBindingKey {}, authority);
        } else {
            deployment_manager::remove_catalog_binding_as<CatalogBindingKey, CatalogBinding>(record, CatalogBindingKey {}, authority);
        };
    }
    public(package) fun begin_customer_receipts(d: &mut DeploymentRecord, authority: address, ctx: &mut TxContext) {
        deployment_manager::begin_catalog_receipts_as<CatalogBindingKey, ShardedCatalogBindingKey, CatalogBinding>(d, CatalogBindingKey {}, ShardedCatalogBindingKey {}, authority, ctx);
    }
    public(package) fun new_for_customer(value: &ComponentVersion, creator: address, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        deployment_manager::new_catalog_as(creator, value.component_name, value.version, groups, devices,
            CatalogBindingKey {}, CatalogBinding { version_id: object::id(value) }, clock, ctx)
    }
    public fun versions(catalog: &ComponentCatalog): &VecMap<String, ID> { &catalog.versions }
    public fun version_publisher(value: &ComponentVersion): address { value.publisher }
    public fun publisher(catalog: &ComponentCatalog): address { catalog.publisher }

    /// alpha: compact private symbol to bound published bytecode storage.
    fun p0(b: u8): bool { (b >= 65 && b <= 90) || (b >= 97 && b <= 122) }
    /// digit: compact private symbol to bound published bytecode storage.
    fun p1(b: u8): bool { b >= 48 && b <= 57 }
    /// validate_token: compact private symbol to bound published bytecode storage.
    fun p2(s: &String, max: u64) {
        let b = s.as_bytes(); p5(b.length() > 0 && b.length() <= max);
        b.do_ref!(|c| p5(p0(*c) || p1(*c) || *c == 45 || *c == 95));
    }
    public fun validate_name(s: &String) {
        let b = s.as_bytes(); assert!(b.length() > 0 && b.length() <= 128, EIdentity);
        b.do_ref!(|c| assert!(p0(*c) || p1(*c) || *c == 45 || *c == 95 || *c == 46, EIdentity));
    }
    public fun validate_hash(s: &String) {
        let b = s.as_bytes(); p5(b.length() == 64);
        b.do_ref!(|c| p5(p1(*c) || (*c >= 97 && *c <= 102)));
    }
    /// valid_os: compact private symbol to bound published bytecode storage.
    fun p3(s: &String): bool {
        *s == string::utf8(b"linux") || *s == string::utf8(b"macos") || *s == string::utf8(b"*")
    }
    /// Rust's `std::env::consts::ARCH` names; `x86` is 32-bit i686 (SF-076).
    /// valid_arch: compact private symbol to bound published bytecode storage.
    fun p4(s: &String): bool {
        *s == string::utf8(b"x86_64") || *s == string::utf8(b"aarch64") || *s == string::utf8(b"arm")
            || *s == string::utf8(b"x86") || *s == string::utf8(b"*")
    }
    /// SemVer 2.0 with bounded u64 core numbers, <=128 ASCII bytes.
    public fun validate_version(s: &String) {
        let b = s.as_bytes(); let n = b.length(); p6(n > 0 && n <= 128);
        let mut i = 0; let mut part = 0u64;
        while (part < 3) {
            let start = i; let mut number: u64 = 0;
            while (i < n && p1(b[i])) {
                let d = (b[i] - 48) as u64;
                p6(number <= (18446744073709551615 - d) / 10);
                number = number * 10 + d; i = i + 1;
            };
            p6(i > start && (i - start == 1 || b[start] != 48));
            part = part + 1;
            if (part < 3) { p6(i < n && b[i] == 46); i = i + 1; };
        };
        if (i == n) return;
        let mut prerelease = b[i] == 45;
        p6(prerelease || b[i] == 43); i = i + 1;
        loop {
            let start = i; let mut numeric = true;
            while (i < n && b[i] != 46 && b[i] != 43) {
                p6(p0(b[i]) || p1(b[i]) || b[i] == 45);
                if (!p1(b[i])) numeric = false;
                i = i + 1;
            };
            p6(i > start);
            if (prerelease && numeric) p6(i - start == 1 || b[start] != 48);
            if (i == n) break;
            if (b[i] == 43) { p6(prerelease); prerelease = false; };
            i = i + 1;
        };
    }
    // Share each named abort branch without changing its module or error code.
    /// check_Manifest: compact private symbol to bound published bytecode storage.
    fun p5(condition: bool) {
        if (!condition) abort EManifest
    }
    /// check_Version: compact private symbol to bound published bytecode storage.
    fun p6(condition: bool) {
        if (!condition) abort EVersion
    }
}
