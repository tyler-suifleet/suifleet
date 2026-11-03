/// Stable customer identity and named managed fleets. Wallets are revocable
/// credentials. Legacy budgets opt in explicitly; adoption never moves devices.
module sui_edge::customer {
    const SMALL_604800000: u32 = 604800000;
    const SMALL_600000: u32 = 600000;
    const SMALL_250000000: u32 = 250000000;
    const SMALL_86400000: u32 = 86400000;
    const SMALL_60000000: u32 = 60000000;

    // Widen named narrow literals at use sites to avoid repeated LdU64.
    // The pinned compiler uses LdU8/U16/U32 followed by CastU64. Values and u64 types
    // are unchanged; abort constants retain their original u64 declarations.
    const SMALL_0: u8 = 0;
    const SMALL_1: u8 = 1;
    const SMALL_2: u8 = 2;
    const SMALL_3: u8 = 3;
    const SMALL_5: u8 = 5;
    const SMALL_6: u8 = 6;
    const SMALL_7: u8 = 7;
    const SMALL_80: u8 = 80;
    const SMALL_196: u8 = 196;
    const SMALL_197: u8 = 197;
    const SMALL_199: u8 = 199;
    const SMALL_201: u8 = 201;
    const SMALL_16384: u16 = 16384;

    use std::string::String;
    use sui_edge::security_policy::{Self, Policy, Action};
    use std::bcs;
    use sui::clock::Clock;
    use sui::coin;
    use sui::dynamic_field as df;
    use sui::dynamic_object_field as dof;
    use sui::event;
    use sui::vec_map::{Self, VecMap};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::device_cap::{Self, DeviceCap};
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::deployment_manager::{Self, DeploymentRecord, RolloutPolicy};
    use sui_edge::desired_state::{Self, RevisionPlan};
    use sui_edge::enrollment::{Self, EnrollmentPolicy};
    use sui_edge::seal_policy::{Self, AccessPolicy, AccessAdminCap};
    use sui_edge::component_catalog::{Self, ComponentCatalog, ComponentVersion};
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasAccount};

    const EAccess: u64 = 0;
    const EBinding: u64 = 1;
    const ELimit: u64 = 2;
    const EName: u64 = 3;
    const ELastAdmin: u64 = 4;
    const EAssigned: u64 = 5;
    const EPeriod: u64 = 6;
    const EBatch: u64 = 7;
    const EArch: u64 = 8;

    /// Most members one batch call may change (SF-171). Each change touches a
    /// few dynamic fields, so the bound keeps a call far below the per-transaction
    /// dynamic-field limit; the dApp also caps the changes per transaction.
    const MAX_GROUP_BATCH: u64 = 100;

    public struct Customer has key {
        id: UID,
        version: u64,
        registry_id: ID,
        name: String,
        credentials: VecMap<address, bool>,
        administrators: u64,
        fleets: vector<ID>,
        default_fleet: Option<ID>,
        active_devices: u64,
        device_limit: u64,
        allowance: u64,
        used: u64,
        period: u64,
        charge_type: vector<u8>,
    }
    public struct Fleet has key {
        id: UID,
        version: u64,
        customer_id: ID,
        name: String,
        budget_id: ID,
        devices: vector<ID>,
        pending_devices: u64,
        groups: vector<ID>,
        deployments: vector<ID>,
        policies: VecMap<ID, ID>,
        enrollment_policies: vector<ID>,
    }
    /// No store: credentials cannot be transferred outside this module.
    public struct Membership has key { id: UID, customer_id: ID }
    public struct CustomerKey has copy, drop, store { wallet: address }
    public struct FleetSecurityKey has copy, drop, store { fleet_id: ID }
    public struct BudgetKey has copy, drop, store {}
    public struct PolicyKey has copy, drop, store { policy_id: ID }
    public struct BudgetLoan { fleet_id: ID, cap_id: ID }
    public struct DeviceKey has copy, drop, store { device_id: ID }
    public struct GroupCountKey has copy, drop, store { device_id: ID }
    public struct AssignmentKey has copy, drop, store { device_id: ID }
    public struct CustomerCreated has copy, drop { customer_id: ID, wallet: address }
    public struct FleetCreated has copy, drop { customer_id: ID, fleet_id: ID, name: String, budget_id: ID }
    public struct CredentialChanged has copy, drop { customer_id: ID, wallet: address, active: bool, administrator: bool }
    public struct DeviceReleased has copy, drop { customer_id: ID, fleet_id: ID, device_id: ID }
    public struct DeviceAssigned has copy, drop { customer_id: ID, fleet_id: ID, device_id: ID }
    /// One batch membership change (SF-171); per-device events are emitted too.
    public struct GroupBatchApplied has copy, drop { fleet_id: ID, group_id: ID, added: bool, count: u64 }

    /// Idempotent per initial credential and registry. A new customer has no
    /// paid entitlement; only registry authority can grant it. There is no billing backend.
    public fun create(registry: &mut DeviceRegistry, name: String, ctx: &mut TxContext) {
        let key = CustomerKey { wallet: ctx.sender() };
        if (df::exists(device_registry::uid(registry), key)) return;
        p8(&name);
        let mut credentials = vec_map::empty();
        credentials.insert(ctx.sender(), true);
        let mut customer = Customer { id: object::new(ctx), version: (SMALL_1 as u64), registry_id: object::id(registry), name,
            credentials, administrators: (SMALL_1 as u64), fleets: vector[], default_fleet: option::none(),
            active_devices: (SMALL_0 as u64), device_limit: (SMALL_0 as u64), allowance: (SMALL_0 as u64), used: (SMALL_0 as u64), period: (SMALL_0 as u64), charge_type: vector[] };
        create_fleet(&mut customer, registry, std::string::utf8(b"Default fleet"), ctx);
        security_policy::initialize(&mut customer.id);
        customer.version = (SMALL_197 as u64);
        let initial_fleet = customer.default_fleet.destroy_some();
        df::add(&mut customer.id, FleetSecurityKey { fleet_id: initial_fleet }, true);
        let customer_id = object::id(&customer);
        df::add(device_registry::uid_mut(registry), key, customer_id);
        transfer::transfer(Membership { id: object::new(ctx), customer_id }, ctx.sender());
        event::emit(CustomerCreated { customer_id, wallet: ctx.sender() });
        transfer::share_object(customer);
    }

    public fun set_credential(customer: &mut Customer, wallet: address, active: bool, administrator: bool, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        pd(wallet != @0x0);
        let revokes_administrator = customer.credentials.contains(&wallet)
            && *customer.credentials.get(&wallet) && (!active || !administrator);
        if (customer.credentials.contains(&wallet)) {
            let (_, was_admin) = customer.credentials.remove(&wallet);
            if (was_admin) customer.administrators = customer.administrators - (SMALL_1 as u64);
        };
        if (active) {
            customer.credentials.insert(wallet, administrator);
            if (administrator) customer.administrators = customer.administrators + (SMALL_1 as u64);
            transfer::transfer(Membership { id: object::new(ctx), customer_id: object::id(customer) }, wallet);
        };
        assert!(customer.administrators > (SMALL_0 as u64), ELastAdmin);
        if (security_authority(customer)) {
            security_policy::invalidate(&mut customer.id);
            if (revokes_administrator) enrollment::invalidate_authority(&mut customer.id);
        };
        event::emit(CredentialChanged { customer_id: object::id(customer), wallet, active, administrator });
    }

    /// Entitlement administration is separate from customer/wallet ownership.
    /// Periods advance monotonically; changing credentials/fleets cannot reset usage.
    public fun set_entitlement(customer: &mut Customer, registry: &DeviceRegistry,
        device_limit: u64, allowance: u64, period: u64, ctx: &mut TxContext) {
        if (!(customer.version == (SMALL_1 as u64))) { pe(security_authority(customer)); }; pe(customer.registry_id == object::id(registry));
        assert!(!df::exists(&customer.id, SubscriptionKey {}), EBinding);
        device_registry::assert_admin(registry, ctx);
        assert!(period >= customer.period, EPeriod);
        pf(device_limit >= customer.active_devices);
        if (period > customer.period) { customer.used = (SMALL_0 as u64); customer.period = period; };
        pf(allowance >= customer.used);
        customer.device_limit = device_limit;
        customer.allowance = allowance;
    }

    /// Subscription custody is additive; this marker never leaves Customer.
    #[test_only]
    public fun subscription_entitlement_for_testing(customer: &Customer): (u64, u64, u64, u64) {
        (customer.device_limit, customer.allowance, customer.used, customer.period)
    }
    public struct SubscriptionKey has copy, drop, store {}
    public(package) fun bind_subscription(customer: &mut Customer, registry: &DeviceRegistry, account: ID, first_period: u64) {
        pe((customer.version == (SMALL_1 as u64) || security_authority(customer)) && customer.registry_id == object::id(registry));
        pe(!df::exists(&customer.id, SubscriptionKey {}));
        // Reject an incompatible legacy grant before taking prepaid custody.
        assert!(first_period > customer.period, EPeriod);
        df::add(&mut customer.id, SubscriptionKey {}, account);
        // Seal beyond both legacy and current security authority versions so old
        // package entry points cannot administer a paid customer. Preserve policy.
        if (customer.version == (SMALL_1 as u64)) security_policy::initialize(&mut customer.id);
        customer.version = (SMALL_201 as u64);
    }
    public(package) fun subscription_entitlement(customer: &mut Customer, account: ID,
        device_limit: u64, allowance: u64, period: u64) {
        pe(*df::borrow<SubscriptionKey, ID>(&customer.id, SubscriptionKey {}) == account);
        assert!(period > customer.period, EPeriod);
        pf(device_limit >= customer.active_devices);
        customer.period = period;
        customer.used = (SMALL_0 as u64);
        customer.device_limit = device_limit;
        customer.allowance = allowance;
    }

    /// Fleet creation has no entitlement side effects. Catalogs may be shared;
    /// every fleet receives a distinct budget and custody namespace.
    public fun create_fleet(customer: &mut Customer, registry: &DeviceRegistry, name: String, ctx: &mut TxContext) {
        p7(customer, (SMALL_6 as u64), ctx);
        pe(customer.registry_id == object::id(registry));
        let (mut budget, cap) = fleet_budget::new_budget(registry, coin::zero(ctx),
            fleet_budget::new_limits((SMALL_60000000 as u64), (SMALL_86400000 as u64), (SMALL_250000000 as u64), (SMALL_6 as u64), (SMALL_600000 as u64)), (SMALL_0 as u64), ctx);
        adopt_budget(customer, &mut budget, cap, name, ctx);
        fleet_budget::share_managed(budget);
    }

    /// Adopt by consuming the exact owner cap. Count existing enrolled devices
    /// immediately; writes stay sealed until each device capability is reconciled.
    /// The first adopted fleet is the default. Retrying with an already consumed
    /// cap cannot duplicate it; callers recover from FleetCreated/customer inventory.
    public fun adopt_budget(customer: &mut Customer, budget: &mut FleetBudget,
        cap: BudgetOwnerCap, name: String, ctx: &mut TxContext) {
        p7(customer, (SMALL_6 as u64), ctx);
        p8(&name);
        pe(fleet_budget::registry_id(budget) == customer.registry_id);
        let count = fleet_budget::device_count(budget);
        if (security_authority(customer) && count > (SMALL_0 as u64)) security_policy::assert_unprotected(&customer.id);
        pf(count <= customer.device_limit - customer.active_devices);
        customer.active_devices = customer.active_devices + count;
        fleet_budget::adopt_managed(budget, &cap);
        let mut fleet = Fleet { id: object::new(ctx), version: (SMALL_1 as u64), customer_id: object::id(customer), name,
            budget_id: object::id(budget), devices: vector[], pending_devices: fleet_budget::device_count(budget), groups: vector[], deployments: vector[], policies: vec_map::empty(), enrollment_policies: vector[] };
        let fleet_id = object::id(&fleet);
        dof::add(&mut fleet.id, BudgetKey {}, cap);
        customer.fleets.push_back(fleet_id);
        if (security_authority(customer) && count == (SMALL_0 as u64)) df::add(&mut customer.id, FleetSecurityKey { fleet_id }, true);
        if (customer.default_fleet.is_none()) customer.default_fleet = option::some(fleet_id);
        event::emit(FleetCreated { customer_id: object::id(customer), fleet_id, name: fleet.name, budget_id: fleet.budget_id });
        transfer::share_object(fleet);
    }

    /// Recipient confirmation is independently signed and bound to this exact
    /// policy generation. It grants no source authority and cannot move assets.
    public struct HandoverCompleted has copy, drop { customer_id: ID, source: address, recipient: address }
    public struct HandoverConsent has key {
        id: UID, customer_id: ID, generation: u64, recipient: address, expires_ms: u64,
    }
    public fun confirm_handover(customer: &Customer, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        pe(security_authority(customer)); pe(expires_ms > clock.timestamp_ms()); pe(expires_ms - clock.timestamp_ms() <= (SMALL_604800000 as u64));
        transfer::share_object(HandoverConsent { id: object::new(ctx), customer_id: object::id(customer),
            generation: security_policy::generation(&security(customer)), recipient: ctx.sender(), expires_ms });
    }
    /// Move the whole organization namespace, retaining budgets, storage caps,
    /// devices and deployments in custody. No partial fleet transfer is implied.
    public fun handover(customer: &mut Customer, consent: HandoverConsent, clock: &Clock, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        let HandoverConsent { id, customer_id, generation, recipient, expires_ms } = consent;
        pe(customer_id == object::id(customer)); pe(security_authority(customer)); pe(generation == security_policy::generation(&security(customer))); pe(clock.timestamp_ms() < expires_ms); pe(recipient != ctx.sender());
        customer.credentials = vec_map::empty();
        customer.credentials.insert(recipient, true);
        customer.administrators = (SMALL_1 as u64);
        transfer::transfer(Membership { id: object::new(ctx), customer_id }, recipient);
        security_policy::invalidate(&mut customer.id);
        enrollment::invalidate_authority(&mut customer.id);
        event::emit(HandoverCompleted { customer_id, source: ctx.sender(), recipient });
        id.delete();
    }

    /// Custody change seals every older catalog publisher entry point.
    public fun adopt_catalog(customer: &Customer, catalog: &mut ComponentCatalog, ctx: &mut TxContext) {
        p7(customer, (SMALL_0 as u64), ctx);
        component_catalog::adopt(catalog, object::id(customer).to_address(), ctx);
    }
    public fun publish_version(customer: &Customer, catalog: &mut ComponentCatalog,
        component_name: String, version: String, platform_os: vector<String>, platform_arch: vector<String>,
        recipe_blob_ids: vector<String>, recipe_sha256s: vector<String>, artifact_sha256s: vector<String>,
        clock: &Clock, ctx: &mut TxContext) {
        p7(customer, (SMALL_0 as u64), ctx);
        component_catalog::publish_as(catalog, component_name, version, platform_os, platform_arch,
            recipe_blob_ids, recipe_sha256s, artifact_sha256s, clock, object::id(customer).to_address(), ctx);
    }
    /// No capability escapes, and both amount and destination are authorized.
    public fun withdraw_budget(customer: &Customer, fleet: &Fleet, budget: &mut FleetBudget,
        amount: u64, recipient: address, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_3 as u64), ctx);
        pe(fleet.pending_devices == (SMALL_0 as u64)); pe(object::id(budget) == fleet.budget_id); pe(recipient != @0x0);
        let cap = dof::borrow(&fleet.id, BudgetKey {});
        transfer::public_transfer(fleet_budget::withdraw(budget, cap, amount, ctx), recipient);
    }

    /// Transaction-scoped capability lending supports the reviewed settings/storage/data
    /// APIs. Enrollment is separately sealed at budget version 2; caps never leave
    /// custody, so revoked wallets cannot retain write authority.
    public fun borrow_budget_cap(customer: &Customer, fleet: &mut Fleet, ctx: &mut TxContext): (BudgetOwnerCap, BudgetLoan) {
        assert_admin(customer, ctx);
        // Generic external authority must never inherit an intent-bound grant.
        if (security_authority(customer)) security_policy::assert_unprotected(&customer.id);
        assert_fleet(customer, fleet);
        pe(fleet.pending_devices == (SMALL_0 as u64));
        let cap = dof::remove(&mut fleet.id, BudgetKey {});
        let loan = BudgetLoan { fleet_id: object::id(fleet), cap_id: object::id(&cap) };
        (cap, loan)
    }
    /// Only the enrolled storage producer can construct W. Its reviewed
    /// operation wrapper computes canonical arguments and never returns W/caps.
    /// This is fixed to withdrawals, with no caller-selected class or factor.
    public fun begin_storage_action<W: drop>(customer: &mut Customer, fleet: &mut Fleet,
        action: Action, arguments_bcs: vector<u8>, clock: &Clock, _source: W, ctx: &mut TxContext): (BudgetOwnerCap, BudgetLoan) {
        assert_security_administrator(customer, ctx); assert_fleet(customer, fleet);
        pd(!customer.charge_type.is_empty()); pd(customer.charge_type == std::type_name::with_original_ids<W>().into_string().into_bytes());
        pe(fleet.pending_devices == (SMALL_0 as u64));
        security_policy::begin_scoped_bytes(&mut customer.id, action, arguments_bcs, (SMALL_3 as u64), false, clock, ctx);
        let cap = dof::remove(&mut fleet.id, BudgetKey {});
        let loan = BudgetLoan { fleet_id: object::id(fleet), cap_id: object::id(&cap) };
        (cap, loan)
    }
    public fun finish_storage_action(customer: &mut Customer, fleet: &mut Fleet, cap: BudgetOwnerCap, loan: BudgetLoan) {
        assert_fleet(customer, fleet);
        return_budget_cap(fleet, cap, loan);
        finish_security_action(customer);
    }

    public fun return_budget_cap(fleet: &mut Fleet, cap: BudgetOwnerCap, loan: BudgetLoan) {
        let BudgetLoan { fleet_id, cap_id } = loan;
        pe(fleet.version == (SMALL_1 as u64)); pe(object::id(fleet) == fleet_id); pe(object::id(&cap) == cap_id);
        dof::add(&mut fleet.id, BudgetKey {}, cap);
    }

    public fun register_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, name: String, device_address: address, arch: String,
        clock: &Clock, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        let device = device_registry::register_device(registry, name, device_address, arch, clock, ctx);
        device_cap::issue_cap(&device, ctx);
        let cap = device_registry::activate_device(registry, device, ctx);
        enroll(customer, fleet, budget, registry, cap, (SMALL_0 as u64), (SMALL_0 as u64), clock, ctx);
    }

    public fun register_funded_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, name: String, device_address: address, arch: String,
        float: u64, reserve: u64, clock: &Clock, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        let device = device_registry::register_device(registry, name, device_address, arch, clock, ctx);
        device_cap::issue_cap(&device, ctx);
        let cap = device_registry::activate_device(registry, device, ctx);
        enroll(customer, fleet, budget, registry, cap, float, reserve, clock, ctx);
    }
    public fun rename_fleet(customer: &Customer, fleet: &mut Fleet, name: String, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx); p8(&name); fleet.name = name;
    }
    public fun set_default_fleet(customer: &mut Customer, fleet: &Fleet, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx); customer.default_fleet = option::some(object::id(fleet));
    }
    /// Explicit atomic reassignment: retire the old gas account, keep identity
    /// and historical records, and enroll in the selected destination budget.
    public fun reassign_device(customer: &mut Customer, source: &mut Fleet, destination: &mut Fleet,
        source_budget: &mut FleetBudget, destination_budget: &mut FleetBudget, account: &mut GasAccount,
        registry: &DeviceRegistry, device_id: ID, clock: &Clock, ctx: &mut TxContext) {
        pe(object::id(source) != object::id(destination));
        let cap = p2(customer, source, source_budget, account, device_id, ctx);
        enroll_checked(customer, destination, destination_budget, registry, cap, (SMALL_0 as u64), (SMALL_0 as u64), clock, ctx);
    }

    public fun enroll(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, cap: DeviceOwnerCap, float: u64, reserve: u64,
        clock: &Clock, ctx: &mut TxContext) {
        p7(customer, (SMALL_6 as u64), ctx);
        let cap = device_registry::rebind_owner(registry, cap, ctx);
        enroll_checked(customer, fleet, budget, registry, cap, float, reserve, clock, ctx);
    }
    fun enroll_checked(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &DeviceRegistry, cap: DeviceOwnerCap, float: u64, reserve: u64, clock: &Clock, ctx: &mut TxContext) {
        assert_fleet(customer, fleet);
        assert!(object::id(budget) == fleet.budget_id && object::id(registry) == customer.registry_id, EBinding);
        let device_id = device_registry::owner_cap_device_id(&cap);
        assert!(!df::exists(&customer.id, AssignmentKey { device_id }), EAssigned);
        assert!(customer.active_devices < customer.device_limit, ELimit);
        assert!(fleet.pending_devices == (SMALL_0 as u64), EBinding);
        let budget_cap = dof::borrow(&fleet.id, BudgetKey {});
        fleet_budget::enroll_managed(budget, budget_cap, registry, &cap, float, reserve, clock, ctx);
        df::add(&mut customer.id, AssignmentKey { device_id }, object::id(fleet));
        dof::add(&mut fleet.id, DeviceKey { device_id }, cap);
        df::add(&mut fleet.id, GroupCountKey { device_id }, (SMALL_0 as u64));
        fleet.devices.push_back(device_id);
        customer.active_devices = customer.active_devices + (SMALL_1 as u64);
        event::emit(DeviceAssigned { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
    }

    public fun create_enrollment_policy(customer: &Customer, fleet: &mut Fleet, registry: &DeviceRegistry, quota: u64, expires_at_ms: u64, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx);
        pe(object::id(registry) == customer.registry_id);
        let policy_id = enrollment::create_managed(registry, object::id(fleet).to_address(), quota, expires_at_ms, ctx);
        fleet.enrollment_policies.push_back(policy_id);
    }
    public fun add_claims(customer: &Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, claims: vector<address>, ctx: &mut TxContext) {
        p0(customer, fleet, policy); p7(customer, (SMALL_2 as u64), ctx);
        enrollment::add_claims_as(policy, claims, object::id(fleet).to_address());
        if (security_authority(customer)) enrollment::bind_security_generation(policy, enrollment::authority_generation(&customer.id));
    }
    public fun revoke_claim(customer: &Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, claim: address, ctx: &mut TxContext) {
        p0(customer, fleet, policy); p7(customer, (SMALL_2 as u64), ctx);
        enrollment::revoke_claim_as(policy, claim, object::id(fleet).to_address());
    }
    public fun configure_enrollment(customer: &Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, quota: u64, expires_at_ms: u64, close: bool, ctx: &mut TxContext) {
        p0(customer, fleet, policy); p7(customer, (SMALL_2 as u64), ctx);
        enrollment::configure_managed(policy, quota, expires_at_ms, close, object::id(fleet).to_address());
    }
    public fun enroll_claim(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget, policy: &mut EnrollmentPolicy,
        registry: &mut DeviceRegistry, name: String, arch: String, clock: &Clock, ctx: &mut TxContext) {
        p0(customer, fleet, policy);
        // Claims delegated under an earlier policy cannot enroll after a
        // enrollment-authority revocation without renewed current authorization.
        if (security_authority(customer)) pe(enrollment::security_generation(policy) == enrollment::authority_generation(&customer.id));
        let cap = enrollment::enroll_managed(policy, registry, name, arch, clock, ctx);
        enroll_checked(customer, fleet, budget, registry, cap, (SMALL_0 as u64), (SMALL_0 as u64), clock, ctx);
    }
    /// assert_enrollment: compact private symbol to bound published bytecode storage.
    fun p0(customer: &Customer, fleet: &Fleet, policy: &EnrollmentPolicy) {
        assert_fleet(customer, fleet);
        pe(fleet.enrollment_policies.contains(&object::id(policy))); pe(enrollment::operator(policy) == object::id(fleet).to_address());
    }
    public fun bind_device_metadata(customer: &Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, alias: String, serial: String, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pe(object::id(registry) == customer.registry_id); pe(fleet.devices.contains(&device_id));
        let cap = dof::borrow(&fleet.id, DeviceKey { device_id });
        let (mut device, loan) = device_registry::borrow_device(registry, cap);
        device_registry::set_meta(&mut device, std::string::utf8(b"suifleet.alias"), alias, ctx);
        device_registry::set_meta(&mut device, std::string::utf8(b"suifleet.serial"), serial, ctx);
        device_registry::return_device(registry, device, loan);
    }
    public fun set_device_revoked(customer: &Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, revoked: bool, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pe(object::id(registry) == customer.registry_id); pe(fleet.devices.contains(&device_id));
        let cap = dof::borrow(&fleet.id, DeviceKey { device_id });
        if (revoked) device_registry::revoke_device(registry, cap) else device_registry::reinstate_device(registry, cap);
    }
    public fun rotate_device_key(customer: &Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, signer: address, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pe(object::id(registry) == customer.registry_id); pe(fleet.devices.contains(&device_id));
        let cap = dof::borrow(&fleet.id, DeviceKey { device_id });
        device_registry::rotate_device_key(registry, cap, signer);
        let (device, loan) = device_registry::borrow_device(registry, cap);
        device_cap::issue_cap(&device, ctx);
        device_registry::return_device(registry, device, loan);
    }

    /// Resumable adoption of each already-enrolled device. Accounting was
    /// reserved at budget adoption, so retries cannot double-count or release it.
    public fun adopt_device(customer: &mut Customer, fleet: &mut Fleet, budget: &FleetBudget,
        account: &mut GasAccount, registry: &mut DeviceRegistry, cap: DeviceOwnerCap, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pe(object::id(budget) == fleet.budget_id); pe(object::id(registry) == customer.registry_id);
        pe(fleet.pending_devices > (SMALL_0 as u64));
        let device_id = device_registry::owner_cap_device_id(&cap);
        pe(fleet_budget::account_id(budget, device_id).is_some());
        let identity = device_registry::identity(registry, device_id).destroy_some();
        pe(device_registry::identity_owner_cap(&identity) == object::id(&cap));
        assert!(!df::exists(&customer.id, AssignmentKey { device_id }), EAssigned);
        pe(fleet_budget::account_id(budget, device_id) == option::some(object::id(account))); pe(fleet_budget::account_owner_cap_id(account) == object::id(&cap));
        let cap = device_registry::rebind_owner(registry, cap, ctx);
        fleet_budget::rebind_account(budget, account, registry, &cap);
        df::add(&mut customer.id, AssignmentKey { device_id }, object::id(fleet));
        dof::add(&mut fleet.id, DeviceKey { device_id }, cap);
        df::add(&mut fleet.id, GroupCountKey { device_id }, (SMALL_0 as u64));
        fleet.devices.push_back(device_id);
        fleet.pending_devices = fleet.pending_devices - (SMALL_1 as u64);
        if (security_authority(customer) && fleet.pending_devices == (SMALL_0 as u64) && !df::exists(&customer.id, FleetSecurityKey { fleet_id: object::id(fleet) })) {
            df::add(&mut customer.id, FleetSecurityKey { fleet_id: object::id(fleet) }, true);
        };
        event::emit(DeviceAssigned { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
    }

    /// Explicit migration recovery, including lost capabilities which cannot be
    /// proven absent on chain. Only an administrator may retire a still-indexed,
    /// unadopted account. Identity and assignments in other fleets are untouched.
    public fun retire_pending_device(customer: &mut Customer, fleet: &mut Fleet,
        budget: &mut FleetBudget, account: &mut GasAccount, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pe(object::id(budget) == fleet.budget_id); pe(fleet.pending_devices > (SMALL_0 as u64));
        let device_id = fleet_budget::account_device_id(account);
        assert!(!fleet.devices.contains(&device_id), EAssigned);
        pe(fleet_budget::account_id(budget, device_id) == option::some(object::id(account)));
        let cap = dof::borrow(&fleet.id, BudgetKey {});
        fleet_budget::remove_managed(budget, cap, account);
        fleet.pending_devices = fleet.pending_devices - (SMALL_1 as u64);
        if (security_authority(customer) && fleet.pending_devices == (SMALL_0 as u64) && !df::exists(&customer.id, FleetSecurityKey { fleet_id: object::id(fleet) })) {
            df::add(&mut customer.id, FleetSecurityKey { fleet_id: object::id(fleet) }, true);
        };
        customer.active_devices = customer.active_devices - (SMALL_1 as u64);
        event::emit(DeviceReleased { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
    }

    /// Adopt a legacy group only after every member is assigned to this fleet.
    /// Updating its stored admin defeats old package mutation entry points too.
    public fun adopt_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup, ctx: &mut TxContext) {
        ph(customer, fleet, ctx);
        pe(fleet.pending_devices == (SMALL_0 as u64));
        let ids = *device_group::device_ids(group);
        p1(fleet, group, ids, ctx);
    }

    /// Explicit complete membership snapshot for an SF-125 indexed group.
    /// Count, uniqueness and on-chain membership prevent hidden foreign members.
    public fun adopt_indexed_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        members: vector<ID>, ctx: &mut TxContext) {
        ph(customer, fleet, ctx);
        pe(fleet.pending_devices == (SMALL_0 as u64));
        p1(fleet, group, members, ctx);
    }

    /// adopt_group_members: compact private symbol to bound published bytecode storage.
    fun p1(fleet: &mut Fleet, group: &mut DeviceGroup,
        ids: vector<ID>, ctx: &mut TxContext) {
        pe(device_group::member_count(group) == ids.length());
        let mut i = (SMALL_0 as u64);
        while (i < ids.length()) {
            pe(device_group::contains_device(group, ids[i]));
            let mut j = (SMALL_0 as u64);
            while (j < i) { pe(ids[j] != ids[i]); j = j + (SMALL_1 as u64); };
            i = i + (SMALL_1 as u64);
        };
        i = (SMALL_0 as u64);
        while (i < ids.length()) { pe(fleet.devices.contains(&ids[i])); i = i + (SMALL_1 as u64); };
        device_group::handover(group, object::id(fleet).to_address(), ctx);
        i = (SMALL_0 as u64);
        while (i < ids.length()) {
            let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id: ids[i] });
            *count = *count + (SMALL_1 as u64); i = i + (SMALL_1 as u64);
        };
        fleet.groups.push_back(object::id(group));
    }

    /// Explicit departure retires the gas account and releases capacity once.
    /// Clear group memberships first; historical deployments/archives stay put.
    /// Return custody to the caller for explicit PTB transfer or composition.
    public fun remove_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, device_id: ID, ctx: &mut TxContext): DeviceOwnerCap {
        p2(customer, fleet, budget, account, device_id, ctx)
    }
    public fun deregister_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, registry: &mut DeviceRegistry, device_id: ID, ctx: &mut TxContext) {
        pe(object::id(registry) == customer.registry_id);
        let cap = p2(customer, fleet, budget, account, device_id, ctx);
        device_registry::deregister_activated_device(registry, cap, ctx);
    }

    /// release_device: compact private symbol to bound published bytecode storage.
    fun p2(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, device_id: ID, ctx: &mut TxContext): DeviceOwnerCap {
        p7(customer, (SMALL_6 as u64), ctx);
        pb(customer, fleet, (SMALL_5 as u64), ctx);
        pe(object::id(budget) == fleet.budget_id); pe(fleet_budget::account_device_id(account) == device_id);
        pe(*df::borrow<GroupCountKey, u64>(&fleet.id, GroupCountKey { device_id }) == (SMALL_0 as u64));
        let cap = dof::borrow(&fleet.id, BudgetKey {});
        fleet_budget::remove_managed(budget, cap, account);
        let owner_cap: DeviceOwnerCap = dof::remove(&mut fleet.id, DeviceKey { device_id });
        let _: u64 = df::remove(&mut fleet.id, GroupCountKey { device_id });
        let assignment: ID = df::remove(&mut customer.id, AssignmentKey { device_id });
        pe(assignment == object::id(fleet));
        let (found, index) = fleet.devices.index_of(&device_id);
        pe(found);
        fleet.devices.swap_remove(index);
        customer.active_devices = customer.active_devices - (SMALL_1 as u64);
        event::emit(DeviceReleased { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
        owner_cap
    }

    public fun create_group(customer: &Customer, fleet: &mut Fleet, name: String,
        description: String, clock: &Clock, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        p8(&name);
        let group = device_group::new_managed(name, description, object::id(fleet).to_address(), clock, ctx);
        fleet.groups.push_back(object::id(&group));
        device_group::share_managed(group);
    }

    public fun add_to_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        device_id: ID, ctx: &mut TxContext) {
        ph(customer, fleet, ctx);
        p6(fleet, group);
        pe(fleet.devices.contains(&device_id));
        device_group::add_managed(group, device_id);
        let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id });
        *count = *count + (SMALL_1 as u64);
    }

    public fun remove_from_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        device_id: ID, ctx: &mut TxContext) {
        ph(customer, fleet, ctx);
        p6(fleet, group);
        device_group::remove_managed(group, device_id);
        let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id });
        *count = *count - (SMALL_1 as u64);
    }

    /// Add up to MAX_GROUP_BATCH fleet devices at once; all or none. A set
    /// `arch` makes the chain re-check every device's registered architecture
    /// against the one the operator reviewed.
    public fun add_many_to_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        registry: &DeviceRegistry, device_ids: vector<ID>, arch: Option<String>, ctx: &mut TxContext) {
        assert!(object::id(registry) == customer.registry_id, EBinding);
        p3(&device_ids);
        let mut i = (SMALL_0 as u64);
        while (i < device_ids.length()) {
            let device_id = device_ids[i];
            if (arch.is_some()) {
                assert!(device_registry::is_custodied(registry, device_id), EArch);
                assert!(device_registry::arch(device_registry::custodied_device(registry, device_id)) == *arch.borrow(), EArch);
            };
            add_to_group(customer, fleet, group, device_id, ctx);
            i = i + (SMALL_1 as u64);
        };
        event::emit(GroupBatchApplied { fleet_id: object::id(fleet), group_id: object::id(group), added: true, count: device_ids.length() });
    }

    /// Remove up to MAX_GROUP_BATCH members at once; all or none.
    public fun remove_many_from_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        device_ids: vector<ID>, ctx: &mut TxContext) {
        p3(&device_ids);
        let mut i = (SMALL_0 as u64);
        while (i < device_ids.length()) { remove_from_group(customer, fleet, group, device_ids[i], ctx); i = i + (SMALL_1 as u64); };
        event::emit(GroupBatchApplied { fleet_id: object::id(fleet), group_id: object::id(group), added: false, count: device_ids.length() });
    }

    /// assert_batch: compact private symbol to bound published bytecode storage.
    fun p3(device_ids: &vector<ID>) {
        assert!(device_ids.length() > (SMALL_0 as u64) && device_ids.length() <= MAX_GROUP_BATCH, EBatch);
    }
    public fun max_group_batch(): u64 { MAX_GROUP_BATCH }

    public fun delete_group(customer: &Customer, fleet: &mut Fleet, group: DeviceGroup, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        p6(fleet, &group);
        pe(device_group::member_count(&group) == (SMALL_0 as u64));
        let (_, index) = fleet.groups.index_of(&object::id(&group));
        fleet.groups.swap_remove(index);
        device_group::delete_managed(group);
    }

    public fun create_deployment(customer: &Customer, fleet: &mut Fleet,
        component_name: String, version: String, recipe_blob_id: String,
        artifact_sha256: String, recipe_sha256: String,
        groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        deployment_manager::share_deployment(new_deployment(customer, fleet, component_name, version, recipe_blob_id,
            artifact_sha256, recipe_sha256, groups, devices, clock, ctx));
    }

    public fun new_deployment(customer: &Customer, fleet: &mut Fleet,
        component_name: String, version: String, recipe_blob_id: String,
        artifact_sha256: String, recipe_sha256: String,
        groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        p7(customer, (SMALL_1 as u64), ctx);
        // Raw recipe publication is also a release, not an alternate catalog bypass.
        pb(customer, fleet, (SMALL_0 as u64), ctx);
        p5(fleet, &groups, &devices);
        let mut record = deployment_manager::new_customer_deployment(object::id(fleet).to_address(),
            component_name, version, recipe_blob_id, artifact_sha256, recipe_sha256, groups, devices, clock, ctx);
        deployment_manager::bind_customer(&mut record, object::id(fleet));
        fleet.deployments.push_back(object::id(&record));
        record
    }

    public fun create_policy(customer: &Customer, fleet: &mut Fleet, name: String, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx);
        let cap = seal_policy::create_policy(name, ctx);
        let policy_id = seal_policy::cap_policy(&cap);
        fleet.policies.insert(policy_id, object::id(&cap));
        dof::add(&mut fleet.id, PolicyKey { policy_id }, cap);
    }
    public fun adopt_policy(customer: &Customer, fleet: &mut Fleet, policy: &AccessPolicy, cap: AccessAdminCap, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx);
        let policy_id = object::id(policy);
        pe(seal_policy::cap_policy(&cap) == policy_id);
        p5(fleet, seal_policy::policy_groups(policy), seal_policy::policy_devices(policy));
        let ids = seal_policy::policy_devices(policy); let mut i = (SMALL_0 as u64);
        while (i < ids.length()) {
            let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id: ids[i] });
            *count = *count + (SMALL_1 as u64); i = i + (SMALL_1 as u64);
        };
        fleet.policies.insert(policy_id, object::id(&cap));
        dof::add(&mut fleet.id, PolicyKey { policy_id }, cap);
    }
    public fun policy_account(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, account: address, grant: bool, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        if (grant) seal_policy::grant_account(policy, cap, account) else seal_policy::revoke_account(policy, cap, account);
    }
    public fun policy_device(customer: &Customer, fleet: &mut Fleet, policy: &mut AccessPolicy, registry: &DeviceRegistry, device_id: ID, grant: bool, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx);
        pe(object::id(registry) == customer.registry_id); pe(fleet.devices.contains(&device_id));
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        if (grant) seal_policy::grant_device(policy, cap, registry, device_id) else seal_policy::revoke_device(policy, cap, device_id);
        let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id });
        if (grant) *count = *count + (SMALL_1 as u64) else *count = *count - (SMALL_1 as u64);
    }
    public fun policy_group(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, group: &DeviceGroup, grant: bool, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx); p6(fleet, group);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        if (grant) seal_policy::grant_managed_group(policy, cap, group) else seal_policy::revoke_group(policy, cap, object::id(group));
    }
    public fun rotate_policy(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        seal_policy::rotate_key_epoch(policy, cap);
    }

    public fun new_catalog_deployment(customer: &Customer, fleet: &mut Fleet, value: &ComponentVersion,
        groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        pb(customer, fleet, (SMALL_1 as u64), ctx);
        // Foreign/legacy immutable releases require publication approval too.
        if (component_catalog::version_publisher(value) != object::id(customer).to_address()) p7(customer, (SMALL_0 as u64), ctx);
        p5(fleet, &groups, &devices);
        let mut record = component_catalog::new_for_customer(value, object::id(fleet).to_address(), groups, devices, clock, ctx);
        deployment_manager::bind_customer(&mut record, object::id(fleet));
        fleet.deployments.push_back(object::id(&record));
        record
    }
    public fun new_rollout(customer: &Customer, fleet: &mut Fleet, component_name: String, version: String,
        recipe_blob_id: String, artifact_sha256: String, recipe_sha256: String, members: vector<ID>, cohorts: vector<u64>,
        policy: RolloutPolicy, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        p7(customer, (SMALL_1 as u64), ctx);
        // Raw recipe publication is also a release, not an alternate catalog bypass.
        pb(customer, fleet, (SMALL_0 as u64), ctx); p5(fleet, &vector[], &members);
        let mut record = deployment_manager::new_rollout_as(object::id(fleet).to_address(), component_name, version,
            recipe_blob_id, artifact_sha256, recipe_sha256, vector[], members, cohorts, policy, clock, ctx);
        deployment_manager::bind_customer(&mut record, object::id(fleet));
        fleet.deployments.push_back(object::id(&record)); record
    }
    public fun advance_rollout(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, online: vector<ID>,
        healthy: vector<ID>, unhealthy: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_1 as u64), ctx);
        pe(fleet.deployments.contains(&object::id(record)));
        deployment_manager::advance_rollout_as(record, online, healthy, unhealthy, clock, object::id(fleet).to_address());
    }
    public fun advance_rollout_onchain(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord,
        registry: &DeviceRegistry, candidates: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_1 as u64), ctx);
        pe(fleet.deployments.contains(&object::id(record)));
        deployment_manager::advance_rollout_onchain_as(record, registry, candidates, clock, object::id(fleet).to_address());
    }
    public fun control_rollout(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, state: u8, skip: vector<ID>, reason: String, clock: &Clock, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_1 as u64), ctx);
        pe(fleet.deployments.contains(&object::id(record)));
        deployment_manager::control_rollout_as(record, state, skip, reason, clock, object::id(fleet).to_address());
    }

    public fun adopt_deployment(customer: &Customer, fleet: &mut Fleet, record: &mut DeploymentRecord, ctx: &mut TxContext) {
        p7(customer, (SMALL_1 as u64), ctx);
        pb(customer, fleet, (SMALL_0 as u64), ctx);
        p5(fleet, deployment_manager::target_groups(record), deployment_manager::target_devices(record));
        deployment_manager::handover(record, object::id(fleet).to_address(), ctx);
        deployment_manager::bind_customer(record, object::id(fleet));
        fleet.deployments.push_back(object::id(record));
    }
    public fun clear_decision_audit(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, limit: u64, ctx: &mut TxContext) {
        pc(customer, fleet, ctx);
        pe(fleet.deployments.contains(&object::id(record)));
        deployment_manager::clear_decision_audit_as(record, limit, object::id(fleet).to_address());
    }
    public fun delete_deployment(customer: &Customer, fleet: &mut Fleet, mut record: DeploymentRecord, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        let (found, index) = fleet.deployments.index_of(&object::id(&record)); pe(found);
        fleet.deployments.swap_remove(index);
        if (deployment_manager::receipt_schema(&record) == (SMALL_2 as u64)) component_catalog::remove_for_customer(&mut record, object::id(fleet).to_address());
        deployment_manager::delete_for_customer(record, object::id(fleet).to_address());
    }
    public fun report_status(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, registry: &DeviceRegistry, cap: &DeviceCap, status: u8, ctx: &mut TxContext) {
        p4(customer, fleet, record, registry, cap, ctx);
        assert!(deployment_manager::target_devices(record).contains(&device_cap::device_id(cap)), EBinding);
        deployment_manager::record_customer_status(record, object::id(fleet), cap, status, ctx);
    }
    public fun report_group_status(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, registry: &DeviceRegistry, cap: &DeviceCap, group: &DeviceGroup, status: u8, ctx: &mut TxContext) {
        p4(customer, fleet, record, registry, cap, ctx); p6(fleet, group);
        pe(deployment_manager::target_groups(record).contains(&object::id(group))); pe(device_group::contains_device(group, device_cap::device_id(cap)));
        deployment_manager::record_customer_status(record, object::id(fleet), cap, status, ctx);
    }
    /// Managed shard operations keep customer/fleet membership checks at every report.
    public fun begin_sharded_receipts(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pe(fleet.deployments.contains(&object::id(record)));
        let authority = object::id(fleet).to_address();
        if (deployment_manager::receipt_schema(record) == (SMALL_2 as u64)) component_catalog::begin_customer_receipts(record, authority, ctx)
        else deployment_manager::begin_sharded_receipts_as(record, authority, ctx);
    }
    public fun begin_receipt_cleanup(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pe(fleet.deployments.contains(&object::id(record)));
        deployment_manager::begin_receipt_cleanup_as(record, object::id(fleet).to_address());
    }
    public fun report_status_v4(customer: &Customer, fleet: &Fleet, record: &DeploymentRecord,
        shard: &mut sui_edge::deployment_manager::ReceiptShard, registry: &DeviceRegistry, cap: &DeviceCap, status: u8, ctx: &mut TxContext) {
        p4(customer, fleet, record, registry, cap, ctx);
        pe(deployment_manager::target_devices(record).contains(&device_cap::device_id(cap)));
        deployment_manager::record_customer_status_v4(record, shard, object::id(fleet), cap, status, ctx);
    }
    public fun report_group_status_v4(customer: &Customer, fleet: &Fleet, record: &DeploymentRecord,
        shard: &mut sui_edge::deployment_manager::ReceiptShard, registry: &DeviceRegistry, cap: &DeviceCap, group: &DeviceGroup, status: u8, ctx: &mut TxContext) {
        p4(customer, fleet, record, registry, cap, ctx); p6(fleet, group);
        pe(deployment_manager::target_groups(record).contains(&object::id(group))); pe(device_group::contains_device(group, device_cap::device_id(cap)));
        deployment_manager::record_customer_status_v4(record, shard, object::id(fleet), cap, status, ctx);
    }
    /// assert_reporter: compact private symbol to bound published bytecode storage.
    fun p4(customer: &Customer, fleet: &Fleet, record: &DeploymentRecord, registry: &DeviceRegistry, cap: &DeviceCap, ctx: &mut TxContext) {
        assert_fleet(customer, fleet);
        pe(object::id(registry) == customer.registry_id); pe(fleet.devices.contains(&device_cap::device_id(cap))); pe(fleet.deployments.contains(&object::id(record))); pe(deployment_manager::creator(record) == object::id(fleet).to_address());
        device_cap::assert_active(cap, registry, ctx);
    }
    public fun revoke_policy_group(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, group_id: ID, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_2 as u64), ctx);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        seal_policy::revoke_group(policy, cap, group_id);
    }

    public fun set_control(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, state: u8,
        not_before_ms: u64, expires_ms: u64, window_start_ms: u64, window_end_ms: u64, notice_ms: u64, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_1 as u64), ctx);
        assert!(fleet.deployments.contains(&object::id(record)), EBinding);
        deployment_manager::set_control_as(record, state, not_before_ms, expires_ms, window_start_ms, window_end_ms, notice_ms, object::id(fleet).to_address());
    }
    public fun set_recurring_window(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, timezone: String,
        days: vector<u8>, start_minutes: vector<u16>, duration_minutes: vector<u16>, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_1 as u64), ctx);
        assert!(fleet.deployments.contains(&object::id(record)), EBinding);
        deployment_manager::set_recurring_window_as(record, timezone, days, start_minutes, duration_minutes, object::id(fleet).to_address());
    }
    public fun clear_recurring_window(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_1 as u64), ctx);
        assert!(fleet.deployments.contains(&object::id(record)), EBinding);
        deployment_manager::clear_recurring_window_as(record, object::id(fleet).to_address());
    }
    public fun begin_revision(customer: &Customer, fleet: &Fleet, group: &DeviceGroup, ctx: &mut TxContext): RevisionPlan {
        pb(customer, fleet, (SMALL_1 as u64), ctx); p6(fleet, group);
        desired_state::begin_as(group, object::id(fleet).to_address())
    }
    public fun commit_revision(customer: &Customer, fleet: &Fleet, plan: RevisionPlan, group: &mut DeviceGroup, clock: &Clock, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_1 as u64), ctx); p6(fleet, group);
        desired_state::commit_as(plan, group, clock, object::id(fleet).to_address(), ctx);
    }
    /// assert_targets: compact private symbol to bound published bytecode storage.
    fun p5(fleet: &Fleet, groups: &vector<ID>, devices: &vector<ID>) {
        let mut i = (SMALL_0 as u64);
        while (i < groups.length()) { pe(fleet.groups.contains(&groups[i])); i = i + (SMALL_1 as u64); };
        i = (SMALL_0 as u64);
        while (i < devices.length()) { pe(fleet.devices.contains(&devices[i])); i = i + (SMALL_1 as u64); };
    }

    /// assert_group: compact private symbol to bound published bytecode storage.
    fun p6(fleet: &Fleet, group: &DeviceGroup) {
        pe(fleet.groups.contains(&object::id(group))); pe(device_group::admin(group) == object::id(fleet).to_address());
    }

    /// Reserve customer allowance before fleet diagnostics/log work. The receipt
    /// is an on-chain counter, never a reset per credential.
    public fun reserve_allowance(customer: &mut Customer, fleet: &Fleet, amount: u64, ctx: &mut TxContext) {
        pb(customer, fleet, (SMALL_6 as u64), ctx);
        pf(amount <= customer.allowance - customer.used);
        customer.used = customer.used + amount;
    }

    /// Device writes consume the same customer ledger as administrator writes.
    /// The charge is serialized on Customer; storage validates its budget first.
    public fun reserve_device_allowance<W: drop>(customer: &mut Customer, fleet: &Fleet, budget: &FleetBudget,
        account: &GasAccount, registry: &DeviceRegistry, cap: &DeviceCap, amount: u64, _witness: W, ctx: &mut TxContext) {
        pd(customer.charge_type == std::type_name::with_original_ids<W>().into_string().into_bytes());
        assert_fleet(customer, fleet);
        pe(fleet.pending_devices == (SMALL_0 as u64)); pe(object::id(budget) == fleet.budget_id);
        pe(fleet.devices.contains(&device_cap::device_id(cap)));
        fleet_budget::assert_may_draw(budget, account, registry, cap, ctx);
        pf(amount <= customer.allowance - customer.used);
        customer.used = customer.used + amount;
    }

    /// Only entitlement authority can authorize an external storage producer.
    /// A non-store, non-publicly-constructible witness then keeps devices from
    /// exhausting the customer ledger by calling its charge function directly.
    public fun configure_charging<W: drop>(customer: &mut Customer, registry: &DeviceRegistry, _witness: W, ctx: &mut TxContext) {
        if (!(customer.version == (SMALL_1 as u64))) { pe(security_authority(customer)); }; pe(object::id(registry) == customer.registry_id);
        if (security_authority(customer)) assert_admin(customer, ctx);
        device_registry::assert_admin(registry, ctx);
        let name = std::type_name::with_original_ids<W>().into_string().into_bytes();
        if (!(customer.charge_type.is_empty())) { pe(customer.charge_type == name); };
        customer.charge_type = name;
    }

    /// Opt in atomically. All-off defaults preserve ordinary current-client use;
    /// old packages can no longer authorize this customer after the version seal.
    fun security_authority(customer: &Customer): bool { customer.version == (SMALL_197 as u64) || customer.version == (SMALL_199 as u64) || customer.version == (SMALL_201 as u64) }
    public fun initialize_security(customer: &mut Customer, ctx: &mut TxContext) {
        pd(customer.credentials.contains(&ctx.sender())); pd(*customer.credentials.get(&ctx.sender()));
        if (security_authority(customer)) { assert_admin(customer, ctx); return };
        if (customer.version == (SMALL_196 as u64)) {
            // Retire the already-published intermediate framework as well as
            // version 1. Its older wrappers must never authorize this state.
            security_policy::check_all(&customer.id, ctx);
            security_policy::invalidate(&mut customer.id);
            enrollment::invalidate_authority(&mut customer.id);
        } else {
            pe(customer.version == (SMALL_1 as u64));
            security_policy::initialize(&mut customer.id);
        };
        customer.version = (SMALL_197 as u64);
    }
    public fun security(customer: &Customer): Policy {
        pe(security_authority(customer));
        security_policy::read(&customer.id)
    }
    /// Existing fleets must prove complete capability reconciliation before a
    /// nonzero policy can be activated. No claim is made for pending old caps.
    public fun seal_security_fleet(customer: &mut Customer, fleet: &Fleet, ctx: &mut TxContext) {
        pc(customer, fleet, ctx);
        pe(security_authority(customer)); pe(fleet.pending_devices == (SMALL_0 as u64));
        let key = FleetSecurityKey { fleet_id: object::id(fleet) };
        if (!df::exists(&customer.id, key)) df::add(&mut customer.id, key, true);
    }
    public fun security_fleet_ready(customer: &Customer, fleet: &Fleet): bool {
        assert_fleet(customer, fleet);
        fleet.pending_devices == (SMALL_0 as u64) && df::exists(&customer.id, FleetSecurityKey { fleet_id: object::id(fleet) })
    }
    public fun set_security(customer: &mut Customer, requirements: vector<u8>, ctx: &mut TxContext) {
        pe(security_authority(customer));
        assert_admin(customer, ctx);
        let mut protected = false;
        let mut i = (SMALL_0 as u64);
        while (i < requirements.length()) { protected = protected || requirements[i] != 0; i = i + (SMALL_1 as u64); };
        if (protected) {
            i = (SMALL_0 as u64);
            while (i < customer.fleets.length()) {
                pe(df::exists(&customer.id, FleetSecurityKey { fleet_id: customer.fleets[i] }));
                i = i + (SMALL_1 as u64);
            };
        };
        let previous = security_policy::requirements(&security(customer));
        security_policy::update(&mut customer.id, requirements, ctx);
        // Re-saving all-off settings changes action generation but preserves
        // provisioned claims. Any change involving protection revokes them.
        if (previous != requirements) enrollment::invalidate_authority(&mut customer.id);
    }
    public fun configure_charging_secured<W: drop>(customer: &mut Customer, registry: &DeviceRegistry,
        source: W, action: Action, clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(registry), bcs::to_bytes(&std::type_name::with_original_ids<W>())];
        begin_intent(customer, action, b"configure_charging", arguments, (SMALL_7 as u64), false, clock, ctx);
        configure_charging(customer, registry, source, ctx);
        finish_security_action(customer);
    }
    public fun prepare_security_action(customer: &Customer, intent: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        security_policy::prepare(&customer.id, intent, expires_ms, clock, ctx);
    }
    /// Any current administrator may cancel without execution factors.
    public fun cancel_security_action(customer: &Customer, action: Action, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        security_policy::cancel(&customer.id, action);
    }
    public fun pending_security_actions(customer: &Customer): vector<ID> { security_policy::pending(&customer.id) }
    public fun cancel_disclosed_security_action(customer: &mut Customer, action: Action, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        security_policy::cancel_disclosed(&mut customer.id, action);
    }
    public(package) fun assert_security_administrator(customer: &Customer, ctx: &mut TxContext) {
        assert_member(customer, ctx);
        pd(security_authority(customer)); pd(*customer.credentials.get(&ctx.sender()));
    }
    /// Trusted child verifiers use these package-only hooks after independently
    /// authenticating the new factor. Public callers cannot construct a grant.
    public(package) fun begin_security_action(customer: &mut Customer, action: Action,
        arguments: vector<vector<u8>>, class: u64, pause: bool, clock: &Clock, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        security_policy::begin_scoped(&mut customer.id, action, arguments, class, pause, clock, ctx);
    }
    public(package) fun finish_security_action(customer: &mut Customer) { security_policy::finish(&mut customer.id); }
    public(package) fun configure_security_factor(customer: &mut Customer, factor: u8, binding: vector<u8>, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        security_policy::configure_factor(&mut customer.id, factor, binding, ctx);
        enrollment::invalidate_authority(&mut customer.id);
    }
    public fun security_factor_configuration(customer: &Customer, factor: u8): vector<u8> {
        pe(security_authority(customer));
        security_policy::factor_configuration(&customer.id, factor)
    }

    /// assert_action: compact private symbol to bound published bytecode storage.
    fun p7(customer: &Customer, action: u64, ctx: &mut TxContext) {
        assert_member(customer, ctx);
        pd(*customer.credentials.get(&ctx.sender()));
        if (security_authority(customer)) security_policy::check(&customer.id, action, ctx);
    }
    public fun assert_member(customer: &Customer, ctx: &mut TxContext) {
        assert!((customer.version == (SMALL_1 as u64) || security_authority(customer)) && customer.credentials.contains(&ctx.sender()), EAccess);
    }
    public fun assert_admin(customer: &Customer, ctx: &mut TxContext) {
        assert_member(customer, ctx);
        pd(*customer.credentials.get(&ctx.sender()));
        if (security_authority(customer)) security_policy::check_all(&customer.id, ctx);
    }
    public fun assert_fleet(customer: &Customer, fleet: &Fleet) {
        if (!(customer.version == (SMALL_1 as u64))) { pe(security_authority(customer)); }; pe(fleet.version == (SMALL_1 as u64)); pe(fleet.customer_id == object::id(customer)); pe(customer.fleets.contains(&object::id(fleet)));
    }
    /// assert_name: compact private symbol to bound published bytecode storage.
    fun p8(name: &String) { assert!(name.length() > (SMALL_0 as u64) && name.length() <= (SMALL_80 as u64), EName); }
    public fun devices(fleet: &Fleet): &vector<ID> { &fleet.devices }
    public fun pending_devices(fleet: &Fleet): u64 { fleet.pending_devices }
    public fun fleet_customer(fleet: &Fleet): ID { fleet.customer_id }
    public fun fleet_budget(fleet: &Fleet): ID { fleet.budget_id }
    public fun fleets(customer: &Customer): &vector<ID> { &customer.fleets }
    public fun active_devices(customer: &Customer): u64 { customer.active_devices }
    public fun used(customer: &Customer): u64 { customer.used }    #[test_only]
    public fun set_security_requirements_for_testing(customer: &mut Customer, requirements: vector<u8>) {
        security_policy::set_for_testing(&mut customer.id, requirements);
    }
    #[test_only]
    public fun set_authority_version_for_testing(customer: &mut Customer, fleet: &mut Fleet, version: u64) {
        customer.version = version; fleet.version = version;
    }

    public fun pause_deployment_secured(customer: &mut Customer, fleet: &Fleet,
        record: &mut DeploymentRecord, action: Action, clock: &Clock, ctx: &mut TxContext) {
        assert_fleet(customer, fleet);
        pe(fleet.deployments.contains(&object::id(record)));
        let arguments = vector[intent_identity(fleet), intent_identity(record), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"pause_deployment", arguments, (SMALL_1 as u64), true, clock, ctx);
        deployment_manager::pause_as(record, object::id(fleet).to_address());
        finish_security_action(customer);
    }

    // Additive intent-bound entry points; see SECOND_FACTOR_DESIGN.md.
    public fun set_credential_secured(customer: &mut Customer, wallet: address, active: bool, administrator: bool, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&wallet), bcs::to_bytes(&active), bcs::to_bytes(&administrator)];
        begin_intent(customer, action, b"set_credential", arguments, (SMALL_7 as u64), false, security_clock, ctx);
        set_credential(customer, wallet, active, administrator, ctx);
        finish_security_action(customer);
    }

    public fun create_fleet_secured(customer: &mut Customer, registry: &DeviceRegistry, name: String, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(registry), bcs::to_bytes(&name)];
        begin_intent(customer, action, b"create_fleet", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        create_fleet(customer, registry, name, ctx);
        finish_security_action(customer);
    }

    public fun adopt_budget_secured(customer: &mut Customer, budget: &mut FleetBudget,
        cap: BudgetOwnerCap, name: String, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(budget), bcs::to_bytes(&object::id(&cap)), bcs::to_bytes(&name)];
        begin_intent(customer, action, b"adopt_budget", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        adopt_budget(customer, budget, cap, name, ctx);
        finish_security_action(customer);
    }

    public fun register_device_secured(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, name: String, device_address: address, arch: String,
        clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(budget), intent_identity(registry), bcs::to_bytes(&name), bcs::to_bytes(&device_address), bcs::to_bytes(&arch)];
        begin_intent(customer, action, b"register_device", arguments, (SMALL_6 as u64), false, clock, ctx);
        register_device(customer, fleet, budget, registry, name, device_address, arch, clock, ctx);
        finish_security_action(customer);
    }

    public fun register_funded_device_secured(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, name: String, device_address: address, arch: String,
        float: u64, reserve: u64, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(budget), intent_identity(registry), bcs::to_bytes(&name), bcs::to_bytes(&device_address), bcs::to_bytes(&arch), bcs::to_bytes(&float), bcs::to_bytes(&reserve)];
        begin_intent(customer, action, b"register_funded_device", arguments, (SMALL_6 as u64), false, clock, ctx);
        register_funded_device(customer, fleet, budget, registry, name, device_address, arch, float, reserve, clock, ctx);
        finish_security_action(customer);
    }

    public fun rename_fleet_secured(customer: &mut Customer, fleet: &mut Fleet, name: String, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&name)];
        begin_intent(customer, action, b"rename_fleet", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        rename_fleet(customer, fleet, name, ctx);
        finish_security_action(customer);
    }

    public fun set_default_fleet_secured(customer: &mut Customer, fleet: &Fleet, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet)];
        begin_intent(customer, action, b"set_default_fleet", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        set_default_fleet(customer, fleet, ctx);
        finish_security_action(customer);
    }

    public fun enroll_secured(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, cap: DeviceOwnerCap, float: u64, reserve: u64,
        clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(budget), intent_identity(registry), bcs::to_bytes(&object::id(&cap)), bcs::to_bytes(&float), bcs::to_bytes(&reserve)];
        begin_intent(customer, action, b"enroll", arguments, (SMALL_6 as u64), false, clock, ctx);
        enroll(customer, fleet, budget, registry, cap, float, reserve, clock, ctx);
        finish_security_action(customer);
    }

    public fun create_enrollment_policy_secured(customer: &mut Customer, fleet: &mut Fleet, registry: &DeviceRegistry, quota: u64, expires_at_ms: u64, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(registry), bcs::to_bytes(&quota), bcs::to_bytes(&expires_at_ms)];
        begin_intent(customer, action, b"create_enrollment_policy", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        create_enrollment_policy(customer, fleet, registry, quota, expires_at_ms, ctx);
        finish_security_action(customer);
    }

    public fun add_claims_secured(customer: &mut Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, claims: vector<address>, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(policy), bcs::to_bytes(&claims), bcs::to_bytes(&enrollment::security_revision(policy))];
        begin_intent(customer, action, b"add_claims", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        add_claims(customer, fleet, policy, claims, ctx);
        finish_security_action(customer);
        enrollment::bind_security_generation(policy, enrollment::authority_generation(&customer.id));
    }

    public fun revoke_claim_secured(customer: &mut Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, claim: address, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(policy), bcs::to_bytes(&claim), bcs::to_bytes(&enrollment::security_revision(policy))];
        begin_intent(customer, action, b"revoke_claim", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        revoke_claim(customer, fleet, policy, claim, ctx);
        finish_security_action(customer);
    }

    public fun configure_enrollment_secured(customer: &mut Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, quota: u64, expires_at_ms: u64, close: bool, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(policy), bcs::to_bytes(&quota), bcs::to_bytes(&expires_at_ms), bcs::to_bytes(&close), bcs::to_bytes(&enrollment::security_revision(policy))];
        begin_intent(customer, action, b"configure_enrollment", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        configure_enrollment(customer, fleet, policy, quota, expires_at_ms, close, ctx);
        finish_security_action(customer);
    }

    public fun bind_device_metadata_secured(customer: &mut Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, alias: String, serial: String, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(registry), bcs::to_bytes(&device_id), bcs::to_bytes(&alias), bcs::to_bytes(&serial)];
        begin_intent(customer, action, b"bind_device_metadata", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        bind_device_metadata(customer, fleet, registry, device_id, alias, serial, ctx);
        finish_security_action(customer);
    }

    public fun set_device_revoked_secured(customer: &mut Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, revoked: bool, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(registry), bcs::to_bytes(&device_id), bcs::to_bytes(&revoked)];
        begin_intent(customer, action, b"set_device_revoked", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        set_device_revoked(customer, fleet, registry, device_id, revoked, ctx);
        finish_security_action(customer);
    }

    public fun rotate_device_key_secured(customer: &mut Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, signer: address, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(registry), bcs::to_bytes(&device_id), bcs::to_bytes(&signer)];
        begin_intent(customer, action, b"rotate_device_key", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        rotate_device_key(customer, fleet, registry, device_id, signer, ctx);
        finish_security_action(customer);
    }

    public fun adopt_device_secured(customer: &mut Customer, fleet: &mut Fleet, budget: &FleetBudget,
        account: &mut GasAccount, registry: &mut DeviceRegistry, cap: DeviceOwnerCap, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(budget), intent_identity(account), intent_identity(registry), bcs::to_bytes(&object::id(&cap))];
        begin_intent(customer, action, b"adopt_device", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        adopt_device(customer, fleet, budget, account, registry, cap, ctx);
        finish_security_action(customer);
    }

    public fun retire_pending_device_secured(customer: &mut Customer, fleet: &mut Fleet,
        budget: &mut FleetBudget, account: &mut GasAccount, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(budget), intent_identity(account)];
        begin_intent(customer, action, b"retire_pending_device", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        retire_pending_device(customer, fleet, budget, account, ctx);
        finish_security_action(customer);
    }

    public fun adopt_group_secured(customer: &mut Customer, fleet: &mut Fleet, group: &mut DeviceGroup, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(group), bcs::to_bytes(&desired_state::revision(group))];
        begin_intent(customer, action, b"adopt_group", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        adopt_group(customer, fleet, group, ctx);
        finish_security_action(customer);
    }

    public fun adopt_indexed_group_secured(customer: &mut Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        members: vector<ID>, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(group), bcs::to_bytes(&members), bcs::to_bytes(&desired_state::revision(group))];
        begin_intent(customer, action, b"adopt_indexed_group", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        adopt_indexed_group(customer, fleet, group, members, ctx);
        finish_security_action(customer);
    }

    public fun deregister_device_secured(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, registry: &mut DeviceRegistry, device_id: ID, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(budget), intent_identity(account), intent_identity(registry), bcs::to_bytes(&device_id)];
        begin_intent(customer, action, b"deregister_device", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        deregister_device(customer, fleet, budget, account, registry, device_id, ctx);
        finish_security_action(customer);
    }

    public fun create_group_secured(customer: &mut Customer, fleet: &mut Fleet, name: String,
        description: String, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&name), bcs::to_bytes(&description)];
        begin_intent(customer, action, b"create_group", arguments, (SMALL_6 as u64), false, clock, ctx);
        create_group(customer, fleet, name, description, clock, ctx);
        finish_security_action(customer);
    }

    public fun add_to_group_secured(customer: &mut Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        device_id: ID, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(group), bcs::to_bytes(&device_id), bcs::to_bytes(&desired_state::revision(group))];
        begin_intent(customer, action, b"add_to_group", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        add_to_group(customer, fleet, group, device_id, ctx);
        finish_security_action(customer);
    }

    public fun remove_from_group_secured(customer: &mut Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        device_id: ID, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(group), bcs::to_bytes(&device_id), bcs::to_bytes(&desired_state::revision(group))];
        begin_intent(customer, action, b"remove_from_group", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        remove_from_group(customer, fleet, group, device_id, ctx);
        finish_security_action(customer);
    }

    public fun delete_group_secured(customer: &mut Customer, fleet: &mut Fleet, group: DeviceGroup, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&object::id(&group))];
        begin_intent(customer, action, b"delete_group", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        delete_group(customer, fleet, group, ctx);
        finish_security_action(customer);
    }

    public fun new_deployment_secured(customer: &mut Customer, fleet: &mut Fleet,
        component_name: String, version: String, recipe_blob_id: String,
        artifact_sha256: String, recipe_sha256: String,
        groups: vector<ID>, devices: vector<ID>, clock: &Clock, action: Action, ctx: &mut TxContext): DeploymentRecord {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&component_name), bcs::to_bytes(&version), bcs::to_bytes(&recipe_blob_id), bcs::to_bytes(&artifact_sha256), bcs::to_bytes(&recipe_sha256), bcs::to_bytes(&groups), bcs::to_bytes(&devices)];
        begin_intent(customer, action, b"new_deployment", arguments, (SMALL_1 as u64), false, clock, ctx);
        let result = new_deployment(customer, fleet, component_name, version, recipe_blob_id, artifact_sha256, recipe_sha256, groups, devices, clock, ctx);
        finish_security_action(customer);
        result
    }

    public fun create_policy_secured(customer: &mut Customer, fleet: &mut Fleet, name: String, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&name)];
        begin_intent(customer, action, b"create_policy", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        create_policy(customer, fleet, name, ctx);
        finish_security_action(customer);
    }

    public fun adopt_policy_secured(customer: &mut Customer, fleet: &mut Fleet, policy: &AccessPolicy, cap: AccessAdminCap, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(policy), bcs::to_bytes(&object::id(&cap))];
        begin_intent(customer, action, b"adopt_policy", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        adopt_policy(customer, fleet, policy, cap, ctx);
        finish_security_action(customer);
    }

    public fun policy_account_secured(customer: &mut Customer, fleet: &Fleet, policy: &mut AccessPolicy, account: address, grant: bool, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(policy), bcs::to_bytes(&account), bcs::to_bytes(&grant)];
        begin_intent(customer, action, b"policy_account", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        policy_account(customer, fleet, policy, account, grant, ctx);
        finish_security_action(customer);
    }

    public fun policy_device_secured(customer: &mut Customer, fleet: &mut Fleet, policy: &mut AccessPolicy, registry: &DeviceRegistry, device_id: ID, grant: bool, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(policy), intent_identity(registry), bcs::to_bytes(&device_id), bcs::to_bytes(&grant)];
        begin_intent(customer, action, b"policy_device", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        policy_device(customer, fleet, policy, registry, device_id, grant, ctx);
        finish_security_action(customer);
    }

    public fun policy_group_secured(customer: &mut Customer, fleet: &Fleet, policy: &mut AccessPolicy, group: &DeviceGroup, grant: bool, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(policy), bcs::to_bytes(group), bcs::to_bytes(&grant), bcs::to_bytes(&desired_state::revision(group))];
        begin_intent(customer, action, b"policy_group", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        policy_group(customer, fleet, policy, group, grant, ctx);
        finish_security_action(customer);
    }

    public fun rotate_policy_secured(customer: &mut Customer, fleet: &Fleet, policy: &mut AccessPolicy, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(policy)];
        begin_intent(customer, action, b"rotate_policy", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        rotate_policy(customer, fleet, policy, ctx);
        finish_security_action(customer);
    }

    public fun new_catalog_deployment_secured(customer: &mut Customer, fleet: &mut Fleet, value: &ComponentVersion,
        groups: vector<ID>, devices: vector<ID>, clock: &Clock, action: Action, ctx: &mut TxContext): DeploymentRecord {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(value), bcs::to_bytes(&groups), bcs::to_bytes(&devices)];
        begin_intent(customer, action, b"new_catalog_deployment", arguments, (SMALL_1 as u64), false, clock, ctx);
        let result = new_catalog_deployment(customer, fleet, value, groups, devices, clock, ctx);
        finish_security_action(customer);
        result
    }

    public fun new_rollout_secured(customer: &mut Customer, fleet: &mut Fleet, component_name: String, version: String,
        recipe_blob_id: String, artifact_sha256: String, recipe_sha256: String, members: vector<ID>, cohorts: vector<u64>,
        policy: RolloutPolicy, clock: &Clock, action: Action, ctx: &mut TxContext): DeploymentRecord {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&component_name), bcs::to_bytes(&version), bcs::to_bytes(&recipe_blob_id), bcs::to_bytes(&artifact_sha256), bcs::to_bytes(&recipe_sha256), bcs::to_bytes(&members), bcs::to_bytes(&cohorts), bcs::to_bytes(&policy)];
        begin_intent(customer, action, b"new_rollout", arguments, (SMALL_1 as u64), false, clock, ctx);
        let result = new_rollout(customer, fleet, component_name, version, recipe_blob_id, artifact_sha256, recipe_sha256, members, cohorts, policy, clock, ctx);
        finish_security_action(customer);
        result
    }

    public fun advance_rollout_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord, online: vector<ID>,
        healthy: vector<ID>, unhealthy: vector<ID>, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), bcs::to_bytes(&online), bcs::to_bytes(&healthy), bcs::to_bytes(&unhealthy), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"advance_rollout", arguments, (SMALL_1 as u64), false, clock, ctx);
        advance_rollout(customer, fleet, record, online, healthy, unhealthy, clock, ctx);
        finish_security_action(customer);
    }

    public fun advance_rollout_onchain_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord,
        registry: &DeviceRegistry, candidates: vector<ID>, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), intent_identity(registry), bcs::to_bytes(&candidates), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"advance_rollout_onchain", arguments, (SMALL_1 as u64), false, clock, ctx);
        advance_rollout_onchain(customer, fleet, record, registry, candidates, clock, ctx);
        finish_security_action(customer);
    }

    public fun control_rollout_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord, state: u8, skip: vector<ID>, reason: String, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), bcs::to_bytes(&state), bcs::to_bytes(&skip), bcs::to_bytes(&reason), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"control_rollout", arguments, (SMALL_1 as u64), false, clock, ctx);
        control_rollout(customer, fleet, record, state, skip, reason, clock, ctx);
        finish_security_action(customer);
    }

    public fun adopt_deployment_secured(customer: &mut Customer, fleet: &mut Fleet, record: &mut DeploymentRecord, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"adopt_deployment", arguments, (SMALL_1 as u64), false, security_clock, ctx);
        adopt_deployment(customer, fleet, record, ctx);
        finish_security_action(customer);
    }

    public fun delete_deployment_secured(customer: &mut Customer, fleet: &mut Fleet, mut record: DeploymentRecord, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&object::id(&record))];
        begin_intent(customer, action, b"delete_deployment", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        delete_deployment(customer, fleet, record, ctx);
        finish_security_action(customer);
    }

    public fun begin_sharded_receipts_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"begin_sharded_receipts", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        begin_sharded_receipts(customer, fleet, record, ctx);
        finish_security_action(customer);
    }

    public fun begin_receipt_cleanup_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"begin_receipt_cleanup", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        begin_receipt_cleanup(customer, fleet, record, ctx);
        finish_security_action(customer);
    }

    public fun revoke_policy_group_secured(customer: &mut Customer, fleet: &Fleet, policy: &mut AccessPolicy, group_id: ID, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(policy), bcs::to_bytes(&group_id)];
        begin_intent(customer, action, b"revoke_policy_group", arguments, (SMALL_2 as u64), false, security_clock, ctx);
        revoke_policy_group(customer, fleet, policy, group_id, ctx);
        finish_security_action(customer);
    }

    public fun set_control_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord, state: u8,
        not_before_ms: u64, expires_ms: u64, window_start_ms: u64, window_end_ms: u64, notice_ms: u64, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), bcs::to_bytes(&state), bcs::to_bytes(&not_before_ms), bcs::to_bytes(&expires_ms), bcs::to_bytes(&window_start_ms), bcs::to_bytes(&window_end_ms), bcs::to_bytes(&notice_ms), deployment_manager::security_snapshot(record)];
        begin_intent(customer, action, b"set_control", arguments, (SMALL_1 as u64), false, security_clock, ctx);
        set_control(customer, fleet, record, state, not_before_ms, expires_ms, window_start_ms, window_end_ms, notice_ms, ctx);
        finish_security_action(customer);
    }

    /// Recurrence wrappers bind customer/fleet/record identities (like
    /// set_control), the deployment snapshot and the current recurring window,
    /// so unrelated usage/enrollment activity cannot invalidate an approval but
    /// an approval cannot be replayed after another schedule change.
    public fun set_recurring_window_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord, timezone: String,
        days: vector<u8>, start_minutes: vector<u16>, duration_minutes: vector<u16>, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), bcs::to_bytes(&timezone), bcs::to_bytes(&days), bcs::to_bytes(&start_minutes), bcs::to_bytes(&duration_minutes), deployment_manager::security_snapshot(record), bcs::to_bytes(&deployment_manager::recurring_window(record))];
        begin_intent(customer, action, b"set_recurring_window", arguments, (SMALL_1 as u64), false, security_clock, ctx);
        set_recurring_window(customer, fleet, record, timezone, days, start_minutes, duration_minutes, ctx);
        finish_security_action(customer);
    }

    public fun clear_recurring_window_secured(customer: &mut Customer, fleet: &Fleet, record: &mut DeploymentRecord, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(record), deployment_manager::security_snapshot(record), bcs::to_bytes(&deployment_manager::recurring_window(record))];
        begin_intent(customer, action, b"clear_recurring_window", arguments, (SMALL_1 as u64), false, security_clock, ctx);
        clear_recurring_window(customer, fleet, record, ctx);
        finish_security_action(customer);
    }

    public fun begin_revision_secured(customer: &mut Customer, fleet: &Fleet, group: &DeviceGroup, action: Action, security_clock: &Clock, ctx: &mut TxContext): RevisionPlan {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(group), bcs::to_bytes(&desired_state::revision(group))];
        begin_intent(customer, action, b"begin_revision", arguments, (SMALL_1 as u64), false, security_clock, ctx);
        let result = begin_revision(customer, fleet, group, ctx);
        finish_security_action(customer);
        result
    }

    public fun commit_revision_secured(customer: &mut Customer, fleet: &Fleet, plan: RevisionPlan, group: &mut DeviceGroup, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&plan), bcs::to_bytes(group), bcs::to_bytes(&desired_state::revision(group))];
        begin_intent(customer, action, b"commit_revision", arguments, (SMALL_1 as u64), false, clock, ctx);
        commit_revision(customer, fleet, plan, group, clock, ctx);
        finish_security_action(customer);
    }

    public fun reserve_allowance_secured(customer: &mut Customer, fleet: &Fleet, amount: u64, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), bcs::to_bytes(&amount)];
        begin_intent(customer, action, b"reserve_allowance", arguments, (SMALL_6 as u64), false, security_clock, ctx);
        reserve_allowance(customer, fleet, amount, ctx);
        finish_security_action(customer);
    }

    public fun set_security_secured(customer: &mut Customer, requirements: vector<u8>, action: Action, security_clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&requirements)];
        begin_intent(customer, action, b"set_security", arguments, (SMALL_7 as u64), false, security_clock, ctx);
        set_security(customer, requirements, ctx);
        finish_security_action(customer);
    }

    public fun adopt_catalog_secured(customer: &mut Customer, catalog: &mut ComponentCatalog, security_clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(catalog)];
        begin_intent(customer, action, b"adopt_catalog", arguments, (SMALL_0 as u64), false, security_clock, ctx);
        adopt_catalog(customer, catalog, ctx);
        finish_security_action(customer);
    }

    public fun publish_version_secured(customer: &mut Customer, catalog: &mut ComponentCatalog, component_name: String, version: String, platform_os: vector<String>, platform_arch: vector<String>, recipe_blob_ids: vector<String>, recipe_sha256s: vector<String>, artifact_sha256s: vector<String>, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(catalog), bcs::to_bytes(&component_name), bcs::to_bytes(&version), bcs::to_bytes(&platform_os), bcs::to_bytes(&platform_arch), bcs::to_bytes(&recipe_blob_ids), bcs::to_bytes(&recipe_sha256s), bcs::to_bytes(&artifact_sha256s)];
        begin_intent(customer, action, b"publish_version", arguments, (SMALL_0 as u64), false, clock, ctx);
        publish_version(customer, catalog, component_name, version, platform_os, platform_arch, recipe_blob_ids, recipe_sha256s, artifact_sha256s, clock, ctx);
        finish_security_action(customer);
    }

    public fun withdraw_budget_secured(customer: &mut Customer, fleet: &Fleet, budget: &mut FleetBudget, amount: u64, recipient: address, security_clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[intent_identity(fleet), intent_identity(budget), bcs::to_bytes(&amount), bcs::to_bytes(&recipient)];
        begin_intent(customer, action, b"withdraw_budget", arguments, (SMALL_3 as u64), false, security_clock, ctx);
        withdraw_budget(customer, fleet, budget, amount, recipient, ctx);
        finish_security_action(customer);
    }

    public fun handover_secured(customer: &mut Customer, consent: HandoverConsent, clock: &Clock, action: Action, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&consent)];
        begin_intent(customer, action, b"handover", arguments, (SMALL_7 as u64), false, clock, ctx);
        handover(customer, consent, clock, ctx);
        finish_security_action(customer);
    }

    /// Leaving managed custody binds the ultimate recipient; it never lends a cap.
    public fun remove_device_secured(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, device_id: ID, recipient: address, action: Action, clock: &Clock, ctx: &mut TxContext) {
        pe(recipient != @0x0);
        let arguments = vector[intent_identity(fleet), intent_identity(budget), intent_identity(account), bcs::to_bytes(&device_id), bcs::to_bytes(&recipient)];
        begin_intent(customer, action, b"remove_device", arguments, (SMALL_7 as u64), false, clock, ctx);
        let cap = remove_device(customer, fleet, budget, account, device_id, ctx);
        transfer::public_transfer(cap, recipient);
        finish_security_action(customer);
    }

    /// assert_fleet_action: compact private symbol to bound published bytecode storage.
    fun pb(customer: &Customer, fleet: &Fleet, action: u64, ctx: &mut TxContext) {
        p7(customer, action, ctx); assert_fleet(customer, fleet);
    }
    /// assert_fleet_admin: compact private symbol to bound published bytecode storage.
    fun pc(customer: &Customer, fleet: &Fleet, ctx: &mut TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
    }
    // Separate guard calls retain short-circuit order and the same abort site/code.
    // Avoid materializing intermediate booleans in the caller bytecode.
    // Share each named abort branch without changing its module or error code.
    /// check_Access: compact private symbol to bound published bytecode storage.
    fun pd(condition: bool) {
        if (!condition) abort EAccess
    }
    /// check_Binding: compact private symbol to bound published bytecode storage.
    fun pe(condition: bool) {
        if (!condition) abort EBinding
    }
    /// check_Limit: compact private symbol to bound published bytecode storage.
    fun pf(condition: bool) {
        if (!condition) abort ELimit
    }

    /// assert_group_actions: compact private symbol to bound published bytecode storage.
    fun ph(customer: &Customer, fleet: &Fleet, ctx: &mut TxContext) {
        p7(customer, (SMALL_6 as u64), ctx);
        p7(customer, (SMALL_1 as u64), ctx);
        pb(customer, fleet, (SMALL_2 as u64), ctx);
    }

    /// Configure all delay classes under the previous policy and delay window.
    public fun configure_security_delays(customer: &mut Customer, milliseconds: vector<u64>, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        security_policy::configure_delays(&mut customer.id, milliseconds, ctx);
        enrollment::invalidate_authority(&mut customer.id);
    }

    public fun configure_security_delays_secured(customer: &mut Customer, milliseconds: vector<u64>, action: Action, clock: &Clock, ctx: &mut TxContext) {
        let arguments = vector[bcs::to_bytes(&milliseconds)];
        begin_intent(customer, action, b"configure_security_delays", arguments, (SMALL_7 as u64), false, clock, ctx);
        configure_security_delays(customer, milliseconds, ctx);
        finish_security_action(customer);
    }

    public fun queue_security_action(customer: &Customer, arguments_bcs: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        security_policy::prepare_with_arguments(&customer.id, arguments_bcs, expires_ms, clock, ctx);
    }

    // Bind the canonical operation and stable identity, excluding device-mutable state.
    public(package) fun begin_intent(customer: &mut Customer, action: Action, name: vector<u8>, tail: vector<vector<u8>>,
        class: u64, pause: bool, clock: &Clock, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        let mut arguments = vector[bcs::to_bytes(&name), intent_identity(customer)];
        arguments.append(tail);
        security_policy::begin_scoped(&mut customer.id, action, arguments, class, pause, clock, ctx);
    }

    fun intent_identity<T: key>(target: &T): vector<u8> { bcs::to_bytes(&object::id(target)) }
    const ESecondPerson: u64 = 9;
    /// Introduction-origin marker and versioned factor binding. Enrollment uses
    /// the same informed proposal/approval protocol as protected operations.
    public struct SecondPersonConfiguration has copy, drop, store { schema: u8 }
    public fun configure_second_person(customer: &mut Customer, action: Action, clock: &Clock, ctx: &mut TxContext) {
        assert!((security_policy::attestations(&action) & 4) == 4, ESecondPerson);
        let arguments = vector[bcs::to_bytes(&std::string::utf8(b"configure_second_person")), intent_identity(customer)];
        begin_security_action(customer, action, arguments, (SMALL_7 as u64), false, clock, ctx);
        configure_security_factor(customer, 4, bcs::to_bytes(&SecondPersonConfiguration { schema: 1 }), ctx);
        finish_security_action(customer);
    }
    public fun propose_approval(customer: &mut Customer, arguments_bcs: vector<u8>, expires_ms: u64, clock: &Clock, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        // Seal before any disclosed Action exists, including enrollment. Old
        // immutable cancellation/execution cannot maintain the pending index.
        assert!(arguments_bcs.length() <= (SMALL_16384 as u64), ESecondPerson);
        let mut encoded = sui::bcs::new(arguments_bcs);
        let arguments = encoded.peel_vec!(|bytes| bytes.peel_vec_u8());
        assert!(encoded.into_remainder_bytes().is_empty() && arguments.length() >= (SMALL_2 as u64)
            && arguments[1] == intent_identity(customer), ESecondPerson);
        if (customer.version == (SMALL_197 as u64)) customer.version = (SMALL_199 as u64);
        let arguments_bcs = bcs::to_bytes(&arguments);
        security_policy::prepare_disclosed(&mut customer.id, arguments_bcs, expires_ms, clock, ctx);
    }
    public fun approve_action(customer: &Customer, action: &mut Action, clock: &Clock, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        assert!(security_policy::customer_id(action) == object::id(customer)
            && security_policy::action_generation(action) == security_policy::generation(&security(customer))
            && security_policy::proposer(action) != ctx.sender() && clock.timestamp_ms() < security_policy::expires_ms(action), ESecondPerson);
        let binding = security_factor_configuration(customer, 4);
        // Before enrollment, approval proves the independent signer works.
        // No nonzero policy can be enabled without a configured verifier.
        assert!(binding.is_empty() || binding == vector[1], ESecondPerson);
        assert!(security_policy::intent(action) == security_policy::action_intent(object::id(customer), security_policy::disclosed_arguments(action)), ESecondPerson);
        security_policy::attest(action, 4);
    }
    public fun reject_action(customer: &mut Customer, action: Action, ctx: &mut TxContext) {
        assert_security_administrator(customer, ctx);
        assert!(security_policy::proposer(&action) != ctx.sender(), ESecondPerson);
        cancel_disclosed_security_action(customer, action, ctx);
    }
}
