/// Immutable authenticated component recipes; manifest order defines precedence.
module sui_edge::component_catalog {
    // Named u64 constants share the bytecode constant pool, including error values,
    // and avoid repeated immediate loads or casts without changing numeric types.
    const SMALL_0: u64 = 0;
    const SMALL_1: u64 = 1;
    const SMALL_2: u64 = 2;
    const SMALL_3: u64 = 3;
    const SMALL_4: u64 = 4;
    const SMALL_10: u64 = 10;
    const SMALL_16: u64 = 16;
    const SMALL_64: u64 = 64;
    const SMALL_128: u64 = 128;

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
        z2(ctx.sender() == catalog.publisher);
        k1(catalog, component_name, version, platform_os, platform_arch, recipe_blob_ids,
            recipe_sha256s, artifact_sha256s, clock, ctx.sender(), ctx);
    }
    /// Custody is an unforgeable customer object address, never a wallet label.
    /// Package helper: adopt.
    public(package) fun k0(catalog: &mut ComponentCatalog, authority: address, ctx: &TxContext) {
        z2(catalog.publisher == ctx.sender());
        catalog.publisher = authority;
    }
    /// Package helper: publish_as.
    public(package) fun k1(
        catalog: &mut ComponentCatalog, component_name: String, version: String,
        platform_os: vector<String>, platform_arch: vector<String>, recipe_blob_ids: vector<String>,
        recipe_sha256s: vector<String>, artifact_sha256s: vector<String>, clock: &Clock, authority: address, ctx: &mut TxContext,
    ) {
        z2(catalog.publisher == authority);
        validate_name(&component_name);
        validate_version(&version);
        let n = platform_os.length();
        z0(n > SMALL_0); z0(n <= SMALL_16); z0(platform_arch.length() == n); z0(recipe_blob_ids.length() == n); z0(recipe_sha256s.length() == n); z0(artifact_sha256s.length() == n);
        let mut i = SMALL_0;
        while (i < n) {
            z0(p3(&platform_os[i])); z0(p4(&platform_arch[i]));
            p2(&recipe_blob_ids[i], SMALL_128);
            validate_hash(&recipe_sha256s[i]); validate_hash(&artifact_sha256s[i]);
            let mut j = SMALL_0;
            while (j < i) {
                z4(platform_os[i] != platform_os[j] || platform_arch[i] != platform_arch[j]);
                j = j + SMALL_1;
            };
            i = i + SMALL_1;
        };
        let mut key = component_name;
        key.append(string::utf8(b"@")); key.append(version);
        z4(!catalog.versions.contains(&key));
        let value = ComponentVersion { id: object::new(ctx), catalog_id: object::id(catalog),
            publisher: authority, component_name, version, schema_version: SMALL_1, created_at: clock.timestamp_ms(),
            platform_os, platform_arch, recipe_blob_ids, recipe_sha256s, artifact_sha256s };
        catalog.versions.insert(key, object::id(&value));
        event::emit(VersionPublished { catalog_id: object::id(catalog), version_id: object::id(&value),
            publisher: authority, component_name, version });
        transfer::freeze_object(value);
    }

    /// Transaction-local catalog record, composable with desired-state revisions.
    public fun new_deployment(value: &ComponentVersion, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        k5(value, ctx.sender(), groups, devices, clock, ctx)
    }

    /// Bindings can only be installed by this module from a frozen version.
    /// Package helper: has_authenticated_version.
    public(package) fun k2(record: &DeploymentRecord): bool {
        deployment_manager::k21<CatalogBindingKey, CatalogBinding>(record, CatalogBindingKey {}) ||
            deployment_manager::k21<ShardedCatalogBindingKey, CatalogBinding>(record, ShardedCatalogBindingKey {})
    }

    public fun create_deployment(value: &ComponentVersion, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        deployment_manager::share_deployment(new_deployment(value, groups, devices, clock, ctx));
    }
    /// Publish with admission paused atomically, so operators can configure a
    /// schedule before any device is eligible to start.
    public fun create_deployment_paused(value: &ComponentVersion, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        let mut record = new_deployment(value, groups, devices, clock, ctx);
        deployment_manager::set_control(&mut record, 1, SMALL_0, SMALL_0, SMALL_0, SMALL_0, SMALL_0, ctx);
        deployment_manager::share_deployment(record);
    }
    #[allow(lint(prefer_mut_tx_context))]
    public fun delete_deployment(mut deployment: DeploymentRecord, ctx: &TxContext) {
        if (deployment_manager::receipt_schema(&deployment) == SMALL_2 &&
            deployment_manager::receipts_version(&deployment) == SMALL_4) {
            deployment_manager::k10<ShardedCatalogBindingKey, CatalogBinding>(&mut deployment, ShardedCatalogBindingKey {}, ctx);
        } else {
            deployment_manager::k10<CatalogBindingKey, CatalogBinding>(&mut deployment, CatalogBindingKey {}, ctx);
        };
        deployment_manager::delete_deployment(deployment, ctx);
    }
    public fun begin_sharded_receipts(d: &mut DeploymentRecord, ctx: &mut TxContext) {
        deployment_manager::k17<CatalogBindingKey, ShardedCatalogBindingKey, CatalogBinding>(
            d, CatalogBindingKey {}, ShardedCatalogBindingKey {}, ctx);
    }
    /// Package helper: remove_for_customer.
    public(package) fun k3(record: &mut DeploymentRecord, authority: address) {
        if (deployment_manager::receipts_version(record) == SMALL_4) {
            deployment_manager::k11<ShardedCatalogBindingKey, CatalogBinding>(record, ShardedCatalogBindingKey {}, authority);
        } else {
            deployment_manager::k11<CatalogBindingKey, CatalogBinding>(record, CatalogBindingKey {}, authority);
        };
    }
    /// Package helper: begin_customer_receipts.
    public(package) fun k4(d: &mut DeploymentRecord, authority: address, ctx: &mut TxContext) {
        deployment_manager::k18<CatalogBindingKey, ShardedCatalogBindingKey, CatalogBinding>(d, CatalogBindingKey {}, ShardedCatalogBindingKey {}, authority, ctx);
    }
    /// Package helper: new_for_customer.
    public(package) fun k5(value: &ComponentVersion, creator: address, groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        deployment_manager::k9(creator, value.component_name, value.version, groups, devices,
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
        let b = s.as_bytes(); z0(b.length() > SMALL_0); z0(b.length() <= max);
        let mut i = SMALL_0;
        while (i < b.length()) { let c = b[i]; z0(p0(c) || p1(c) || c == 45 || c == 95); i = i + SMALL_1; };
    }
    public fun validate_name(s: &String) {
        let b = s.as_bytes(); z3(b.length() > SMALL_0); z3(b.length() <= SMALL_128);
        let mut i = SMALL_0;
        while (i < b.length()) { let c = b[i]; z3(p0(c) || p1(c) || c == 45 || c == 95 || c == 46); i = i + SMALL_1; };
    }
    public fun validate_hash(s: &String) {
        let b = s.as_bytes(); z0(b.length() == SMALL_64);
        let mut i = SMALL_0;
        while (i < b.length()) { let c = b[i]; z0(p1(c) || (c >= 97 && c <= 102)); i = i + SMALL_1; };
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
        let b = s.as_bytes(); let n = b.length(); z1(n > SMALL_0); z1(n <= SMALL_128);
        let mut i = SMALL_0; let mut part = SMALL_0;
        while (part < SMALL_3) {
            let start = i; let mut number: u64 = SMALL_0;
            while (i < n && p1(b[i])) {
                let d = (b[i] - 48) as u64;
                z1(number <= (18446744073709551615 - d) / SMALL_10);
                number = number * SMALL_10 + d; i = i + SMALL_1;
            };
            z1(i > start); if (!(i - start == SMALL_1)) { z1(b[start] != 48); };
            part = part + SMALL_1;
            if (part < SMALL_3) { z1(i < n); z1(b[i] == 46); i = i + SMALL_1; };
        };
        if (i == n) return;
        let mut prerelease = b[i] == 45;
        if (!(prerelease)) { z1(b[i] == 43); }; i = i + SMALL_1;
        loop {
            let start = i; let mut numeric = true;
            while (i < n && b[i] != 46 && b[i] != 43) {
                if (!(p0(b[i]))) { if (!(p1(b[i]))) { z1(b[i] == 45); }; };
                if (!p1(b[i])) numeric = false;
                i = i + SMALL_1;
            };
            z1(i > start);
            if (prerelease && numeric) if (!(i - start == SMALL_1)) { z1(b[start] != 48); };
            if (i == n) break;
            if (b[i] == 43) { z1(prerelease); prerelease = false; };
            i = i + SMALL_1;
        };
    }
    // Separate guard calls retain short-circuit order and the same abort site/code.
    // Avoid materializing intermediate booleans in the caller bytecode.
    // Share each named abort branch without changing its module or error code.

    // Private named guards retain module-local error codes; selectors are fixed at each call.

    fun z0(condition: bool) { if (!condition) abort EManifest }
    fun z1(condition: bool) { if (!condition) abort EVersion }
    fun z2(condition: bool) { if (!condition) abort EPublisher }
    fun z3(condition: bool) { if (!condition) abort EIdentity }
    fun z4(condition: bool) { if (!condition) abort EDuplicate }
}
