/// Stable customer identity and named managed fleets. Wallets are revocable
/// credentials. Legacy budgets opt in explicitly; adoption never moves devices.
module sui_edge::customer {
    use std::string::String;
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
    use sui_edge::component_catalog::{Self, ComponentVersion};
    use sui_edge::fleet_budget::{Self, FleetBudget, BudgetOwnerCap, GasAccount};

    const EAccess: u64 = 0;
    const EBinding: u64 = 1;
    const ELimit: u64 = 2;
    const EName: u64 = 3;
    const ELastAdmin: u64 = 4;
    const EAssigned: u64 = 5;
    const EPeriod: u64 = 6;

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

    /// Idempotent per initial credential and registry. A new customer has no
    /// paid entitlement; only registry authority can grant it. There is no billing backend.
    public fun create(registry: &mut DeviceRegistry, name: String, ctx: &mut TxContext) {
        let key = CustomerKey { wallet: ctx.sender() };
        if (df::exists(device_registry::uid(registry), key)) return;
        assert_name(&name);
        let mut credentials = vec_map::empty();
        credentials.insert(ctx.sender(), true);
        let mut customer = Customer { id: object::new(ctx), version: 1, registry_id: object::id(registry), name,
            credentials, administrators: 1, fleets: vector[], default_fleet: option::none(),
            active_devices: 0, device_limit: 0, allowance: 0, used: 0, period: 0, charge_type: vector[] };
        create_fleet(&mut customer, registry, std::string::utf8(b"Default fleet"), ctx);
        let customer_id = object::id(&customer);
        df::add(device_registry::uid_mut(registry), key, customer_id);
        transfer::transfer(Membership { id: object::new(ctx), customer_id }, ctx.sender());
        event::emit(CustomerCreated { customer_id, wallet: ctx.sender() });
        transfer::share_object(customer);
    }

    public fun set_credential(customer: &mut Customer, wallet: address, active: bool, administrator: bool, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert!(wallet != @0x0, EAccess);
        if (customer.credentials.contains(&wallet)) {
            let (_, was_admin) = customer.credentials.remove(&wallet);
            if (was_admin) customer.administrators = customer.administrators - 1;
        };
        if (active) {
            customer.credentials.insert(wallet, administrator);
            if (administrator) customer.administrators = customer.administrators + 1;
            transfer::transfer(Membership { id: object::new(ctx), customer_id: object::id(customer) }, wallet);
        };
        assert!(customer.administrators > 0, ELastAdmin);
        event::emit(CredentialChanged { customer_id: object::id(customer), wallet, active, administrator });
    }

    /// Entitlement administration is separate from customer/wallet ownership.
    /// Periods advance monotonically; changing credentials/fleets cannot reset usage.
    public fun set_entitlement(customer: &mut Customer, registry: &DeviceRegistry,
        device_limit: u64, allowance: u64, period: u64, ctx: &TxContext) {
        assert!(customer.version == 1 && customer.registry_id == object::id(registry), EBinding);
        device_registry::assert_admin(registry, ctx);
        assert!(period >= customer.period, EPeriod);
        assert!(device_limit >= customer.active_devices, ELimit);
        if (period > customer.period) { customer.used = 0; customer.period = period; };
        assert!(allowance >= customer.used, ELimit);
        customer.device_limit = device_limit;
        customer.allowance = allowance;
    }

    /// Fleet creation has no entitlement side effects. Catalogs may be shared;
    /// every fleet receives a distinct budget and custody namespace.
    public fun create_fleet(customer: &mut Customer, registry: &DeviceRegistry, name: String, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert!(customer.registry_id == object::id(registry), EBinding);
        let (mut budget, cap) = fleet_budget::new_budget(registry, coin::zero(ctx),
            fleet_budget::new_limits(60_000_000, 86_400_000, 250_000_000, 6, 600_000), 0, ctx);
        adopt_budget(customer, &mut budget, cap, name, ctx);
        fleet_budget::share_managed(budget);
    }

    /// Adopt by consuming the exact owner cap. Count existing enrolled devices
    /// immediately; writes stay sealed until each device capability is reconciled.
    /// The first adopted fleet is the default. Retrying with an already consumed
    /// cap cannot duplicate it; callers recover from FleetCreated/customer inventory.
    public fun adopt_budget(customer: &mut Customer, budget: &mut FleetBudget,
        cap: BudgetOwnerCap, name: String, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert_name(&name);
        assert!(fleet_budget::registry_id(budget) == customer.registry_id, EBinding);
        let count = fleet_budget::device_count(budget);
        assert!(count <= customer.device_limit - customer.active_devices, ELimit);
        customer.active_devices = customer.active_devices + count;
        fleet_budget::adopt_managed(budget, &cap);
        let mut fleet = Fleet { id: object::new(ctx), version: 1, customer_id: object::id(customer), name,
            budget_id: object::id(budget), devices: vector[], pending_devices: fleet_budget::device_count(budget), groups: vector[], deployments: vector[], policies: vec_map::empty(), enrollment_policies: vector[] };
        let fleet_id = object::id(&fleet);
        dof::add(&mut fleet.id, BudgetKey {}, cap);
        customer.fleets.push_back(fleet_id);
        if (customer.default_fleet.is_none()) customer.default_fleet = option::some(fleet_id);
        event::emit(FleetCreated { customer_id: object::id(customer), fleet_id, name: fleet.name, budget_id: fleet.budget_id });
        transfer::share_object(fleet);
    }

    /// Transaction-scoped capability lending supports the reviewed settings/storage/data
    /// APIs. Enrollment is separately sealed at budget version 2; caps never leave
    /// custody, so revoked wallets cannot retain write authority.
    public fun borrow_budget_cap(customer: &Customer, fleet: &mut Fleet, ctx: &TxContext): (BudgetOwnerCap, BudgetLoan) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert!(fleet.pending_devices == 0, EBinding);
        let cap = dof::remove(&mut fleet.id, BudgetKey {});
        let loan = BudgetLoan { fleet_id: object::id(fleet), cap_id: object::id(&cap) };
        (cap, loan)
    }
    public fun return_budget_cap(fleet: &mut Fleet, cap: BudgetOwnerCap, loan: BudgetLoan) {
        let BudgetLoan { fleet_id, cap_id } = loan;
        assert!(fleet.version == 1 && object::id(fleet) == fleet_id && object::id(&cap) == cap_id, EBinding);
        dof::add(&mut fleet.id, BudgetKey {}, cap);
    }

    public fun register_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, name: String, device_address: address, arch: String,
        clock: &Clock, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        let device = device_registry::register_device(registry, name, device_address, arch, clock, ctx);
        device_cap::issue_cap(&device, ctx);
        let cap = device_registry::activate_device(registry, device, ctx);
        enroll(customer, fleet, budget, registry, cap, 0, 0, clock, ctx);
    }

    public fun register_funded_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, name: String, device_address: address, arch: String,
        float: u64, reserve: u64, clock: &Clock, ctx: &mut TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        let device = device_registry::register_device(registry, name, device_address, arch, clock, ctx);
        device_cap::issue_cap(&device, ctx);
        let cap = device_registry::activate_device(registry, device, ctx);
        enroll(customer, fleet, budget, registry, cap, float, reserve, clock, ctx);
    }
    public fun rename_fleet(customer: &Customer, fleet: &mut Fleet, name: String, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet); assert_name(&name); fleet.name = name;
    }
    public fun set_default_fleet(customer: &mut Customer, fleet: &Fleet, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet); customer.default_fleet = option::some(object::id(fleet));
    }
    /// Explicit atomic reassignment: retire the old gas account, keep identity
    /// and historical records, and enroll in the selected destination budget.
    public fun reassign_device(customer: &mut Customer, source: &mut Fleet, destination: &mut Fleet,
        source_budget: &mut FleetBudget, destination_budget: &mut FleetBudget, account: &mut GasAccount,
        registry: &DeviceRegistry, device_id: ID, clock: &Clock, ctx: &mut TxContext) {
        assert!(object::id(source) != object::id(destination), EBinding);
        let cap = release_device(customer, source, source_budget, account, device_id, ctx);
        enroll_checked(customer, destination, destination_budget, registry, cap, 0, 0, clock, ctx);
    }

    public fun enroll(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &mut DeviceRegistry, cap: DeviceOwnerCap, float: u64, reserve: u64,
        clock: &Clock, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
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
        assert!(fleet.pending_devices == 0, EBinding);
        let budget_cap = dof::borrow(&fleet.id, BudgetKey {});
        fleet_budget::enroll_managed(budget, budget_cap, registry, &cap, float, reserve, clock, ctx);
        df::add(&mut customer.id, AssignmentKey { device_id }, object::id(fleet));
        dof::add(&mut fleet.id, DeviceKey { device_id }, cap);
        df::add(&mut fleet.id, GroupCountKey { device_id }, 0u64);
        fleet.devices.push_back(device_id);
        customer.active_devices = customer.active_devices + 1;
        event::emit(DeviceAssigned { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
    }

    public fun create_enrollment_policy(customer: &Customer, fleet: &mut Fleet, registry: &DeviceRegistry, quota: u64, expires_at_ms: u64, ctx: &mut TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(object::id(registry) == customer.registry_id, EBinding);
        let policy_id = enrollment::create_managed(registry, object::id(fleet).to_address(), quota, expires_at_ms, ctx);
        fleet.enrollment_policies.push_back(policy_id);
    }
    public fun add_claims(customer: &Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, claims: vector<address>, ctx: &TxContext) {
        assert_enrollment(customer, fleet, policy); assert_admin(customer, ctx);
        enrollment::add_claims_as(policy, claims, object::id(fleet).to_address());
    }
    public fun revoke_claim(customer: &Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, claim: address, ctx: &TxContext) {
        assert_enrollment(customer, fleet, policy); assert_admin(customer, ctx);
        enrollment::revoke_claim_as(policy, claim, object::id(fleet).to_address());
    }
    public fun configure_enrollment(customer: &Customer, fleet: &Fleet, policy: &mut EnrollmentPolicy, quota: u64, expires_at_ms: u64, close: bool, ctx: &TxContext) {
        assert_enrollment(customer, fleet, policy); assert_admin(customer, ctx);
        enrollment::configure_managed(policy, quota, expires_at_ms, close, object::id(fleet).to_address());
    }
    public fun enroll_claim(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget, policy: &mut EnrollmentPolicy,
        registry: &mut DeviceRegistry, name: String, arch: String, clock: &Clock, ctx: &mut TxContext) {
        assert_enrollment(customer, fleet, policy);
        let cap = enrollment::enroll_managed(policy, registry, name, arch, clock, ctx);
        enroll_checked(customer, fleet, budget, registry, cap, 0, 0, clock, ctx);
    }
    fun assert_enrollment(customer: &Customer, fleet: &Fleet, policy: &EnrollmentPolicy) {
        assert_fleet(customer, fleet);
        assert!(fleet.enrollment_policies.contains(&object::id(policy)) && enrollment::operator(policy) == object::id(fleet).to_address(), EBinding);
    }
    public fun bind_device_metadata(customer: &Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, alias: String, serial: String, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(object::id(registry) == customer.registry_id && fleet.devices.contains(&device_id), EBinding);
        let cap = dof::borrow(&fleet.id, DeviceKey { device_id });
        let (mut device, loan) = device_registry::borrow_device(registry, cap);
        device_registry::set_meta(&mut device, std::string::utf8(b"suifleet.alias"), alias, ctx);
        device_registry::set_meta(&mut device, std::string::utf8(b"suifleet.serial"), serial, ctx);
        device_registry::return_device(registry, device, loan);
    }
    public fun set_device_revoked(customer: &Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, revoked: bool, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(object::id(registry) == customer.registry_id && fleet.devices.contains(&device_id), EBinding);
        let cap = dof::borrow(&fleet.id, DeviceKey { device_id });
        if (revoked) device_registry::revoke_device(registry, cap) else device_registry::reinstate_device(registry, cap);
    }
    public fun rotate_device_key(customer: &Customer, fleet: &Fleet, registry: &mut DeviceRegistry, device_id: ID, signer: address, ctx: &mut TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(object::id(registry) == customer.registry_id && fleet.devices.contains(&device_id), EBinding);
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
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert!(object::id(budget) == fleet.budget_id && object::id(registry) == customer.registry_id, EBinding);
        assert!(fleet.pending_devices > 0, EBinding);
        let device_id = device_registry::owner_cap_device_id(&cap);
        assert!(fleet_budget::account_id(budget, device_id).is_some(), EBinding);
        let identity = device_registry::identity(registry, device_id).destroy_some();
        assert!(device_registry::identity_owner_cap(&identity) == object::id(&cap), EBinding);
        assert!(!df::exists(&customer.id, AssignmentKey { device_id }), EAssigned);
        assert!(fleet_budget::account_id(budget, device_id) == option::some(object::id(account)) && fleet_budget::account_owner_cap_id(account) == object::id(&cap), EBinding);
        let cap = device_registry::rebind_owner(registry, cap, ctx);
        fleet_budget::rebind_account(budget, account, registry, &cap);
        df::add(&mut customer.id, AssignmentKey { device_id }, object::id(fleet));
        dof::add(&mut fleet.id, DeviceKey { device_id }, cap);
        df::add(&mut fleet.id, GroupCountKey { device_id }, 0u64);
        fleet.devices.push_back(device_id);
        fleet.pending_devices = fleet.pending_devices - 1;
        event::emit(DeviceAssigned { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
    }

    /// Adopt a legacy group only after every member is assigned to this fleet.
    /// Updating its stored admin defeats old package mutation entry points too.
    public fun adopt_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup, ctx: &TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert!(fleet.pending_devices == 0, EBinding);
        let ids = *device_group::device_ids(group);
        let mut i = 0;
        while (i < ids.length()) { assert!(fleet.devices.contains(&ids[i]), EBinding); i = i + 1; };
        device_group::handover(group, object::id(fleet).to_address(), ctx);
        i = 0;
        while (i < ids.length()) {
            let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id: ids[i] });
            *count = *count + 1; i = i + 1;
        };
        fleet.groups.push_back(object::id(group));
    }

    /// Explicit departure retires the gas account and releases capacity once.
    /// Clear group memberships first; historical deployments/archives stay put.
    public fun remove_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, device_id: ID, ctx: &TxContext) {
        let cap = release_device(customer, fleet, budget, account, device_id, ctx);
        transfer::public_transfer(cap, ctx.sender());
    }
    public fun deregister_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, registry: &mut DeviceRegistry, device_id: ID, ctx: &TxContext) {
        assert!(object::id(registry) == customer.registry_id, EBinding);
        let cap = release_device(customer, fleet, budget, account, device_id, ctx);
        device_registry::deregister_activated_device(registry, cap, ctx);
    }

    fun release_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, device_id: ID, ctx: &TxContext): DeviceOwnerCap {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert!(object::id(budget) == fleet.budget_id && fleet_budget::account_device_id(account) == device_id, EBinding);
        assert!(*df::borrow<GroupCountKey, u64>(&fleet.id, GroupCountKey { device_id }) == 0, EBinding);
        let cap = dof::borrow(&fleet.id, BudgetKey {});
        fleet_budget::remove_managed(budget, cap, account);
        let owner_cap: DeviceOwnerCap = dof::remove(&mut fleet.id, DeviceKey { device_id });
        let _: u64 = df::remove(&mut fleet.id, GroupCountKey { device_id });
        let assignment: ID = df::remove(&mut customer.id, AssignmentKey { device_id });
        assert!(assignment == object::id(fleet), EBinding);
        let (found, index) = fleet.devices.index_of(&device_id);
        assert!(found, EBinding);
        fleet.devices.swap_remove(index);
        customer.active_devices = customer.active_devices - 1;
        event::emit(DeviceReleased { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
        owner_cap
    }

    public fun create_group(customer: &Customer, fleet: &mut Fleet, name: String,
        description: String, clock: &Clock, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert_name(&name);
        let group = device_group::new_managed(name, description, object::id(fleet).to_address(), clock, ctx);
        fleet.groups.push_back(object::id(&group));
        device_group::share_managed(group);
    }

    public fun add_to_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        device_id: ID, ctx: &TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert_group(fleet, group);
        assert!(fleet.devices.contains(&device_id), EBinding);
        device_group::add_managed(group, device_id);
        let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id });
        *count = *count + 1;
    }

    public fun remove_from_group(customer: &Customer, fleet: &mut Fleet, group: &mut DeviceGroup,
        device_id: ID, ctx: &TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert_group(fleet, group);
        device_group::remove_managed(group, device_id);
        let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id });
        *count = *count - 1;
    }

    public fun delete_group(customer: &Customer, fleet: &mut Fleet, group: DeviceGroup, ctx: &TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert_group(fleet, &group);
        assert!(device_group::device_ids(&group).is_empty(), EBinding);
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
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        let mut i = 0;
        while (i < groups.length()) { assert!(fleet.groups.contains(&groups[i]), EBinding); i = i + 1; };
        i = 0;
        while (i < devices.length()) { assert!(fleet.devices.contains(&devices[i]), EBinding); i = i + 1; };
        let mut record = deployment_manager::new_customer_deployment(object::id(fleet).to_address(),
            component_name, version, recipe_blob_id, artifact_sha256, recipe_sha256, groups, devices, clock, ctx);
        deployment_manager::bind_customer(&mut record, object::id(fleet));
        fleet.deployments.push_back(object::id(&record));
        record
    }

    public fun create_policy(customer: &Customer, fleet: &mut Fleet, name: String, ctx: &mut TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        let cap = seal_policy::create_policy(name, ctx);
        let policy_id = seal_policy::cap_policy(&cap);
        fleet.policies.insert(policy_id, object::id(&cap));
        dof::add(&mut fleet.id, PolicyKey { policy_id }, cap);
    }
    public fun adopt_policy(customer: &Customer, fleet: &mut Fleet, policy: &AccessPolicy, cap: AccessAdminCap, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        let policy_id = object::id(policy);
        assert!(seal_policy::cap_policy(&cap) == policy_id, EBinding);
        assert_targets(fleet, seal_policy::policy_groups(policy), seal_policy::policy_devices(policy));
        let ids = seal_policy::policy_devices(policy); let mut i = 0;
        while (i < ids.length()) {
            let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id: ids[i] });
            *count = *count + 1; i = i + 1;
        };
        fleet.policies.insert(policy_id, object::id(&cap));
        dof::add(&mut fleet.id, PolicyKey { policy_id }, cap);
    }
    public fun policy_account(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, account: address, grant: bool, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        if (grant) seal_policy::grant_account(policy, cap, account) else seal_policy::revoke_account(policy, cap, account);
    }
    public fun policy_device(customer: &Customer, fleet: &mut Fleet, policy: &mut AccessPolicy, registry: &DeviceRegistry, device_id: ID, grant: bool, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(object::id(registry) == customer.registry_id && fleet.devices.contains(&device_id), EBinding);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        if (grant) seal_policy::grant_device(policy, cap, registry, device_id) else seal_policy::revoke_device(policy, cap, device_id);
        let count: &mut u64 = df::borrow_mut(&mut fleet.id, GroupCountKey { device_id });
        if (grant) *count = *count + 1 else *count = *count - 1;
    }
    public fun policy_group(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, group: &DeviceGroup, grant: bool, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet); assert_group(fleet, group);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        if (grant) seal_policy::grant_managed_group(policy, cap, group) else seal_policy::revoke_group(policy, cap, object::id(group));
    }
    public fun rotate_policy(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        seal_policy::rotate_key_epoch(policy, cap);
    }

    public fun new_catalog_deployment(customer: &Customer, fleet: &mut Fleet, value: &ComponentVersion,
        groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert_targets(fleet, &groups, &devices);
        let mut record = component_catalog::new_for_customer(value, object::id(fleet).to_address(), groups, devices, clock, ctx);
        deployment_manager::bind_customer(&mut record, object::id(fleet));
        fleet.deployments.push_back(object::id(&record));
        record
    }
    public fun new_rollout(customer: &Customer, fleet: &mut Fleet, component_name: String, version: String,
        recipe_blob_id: String, artifact_sha256: String, recipe_sha256: String, members: vector<ID>, cohorts: vector<u64>,
        policy: RolloutPolicy, clock: &Clock, ctx: &mut TxContext): DeploymentRecord {
        assert_admin(customer, ctx); assert_fleet(customer, fleet); assert_targets(fleet, &vector[], &members);
        let mut record = deployment_manager::new_rollout_as(object::id(fleet).to_address(), component_name, version,
            recipe_blob_id, artifact_sha256, recipe_sha256, vector[], members, cohorts, policy, clock, ctx);
        deployment_manager::bind_customer(&mut record, object::id(fleet));
        fleet.deployments.push_back(object::id(&record)); record
    }
    public fun advance_rollout(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, online: vector<ID>,
        healthy: vector<ID>, unhealthy: vector<ID>, clock: &Clock, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(fleet.deployments.contains(&object::id(record)), EBinding);
        deployment_manager::advance_rollout_as(record, online, healthy, unhealthy, clock, object::id(fleet).to_address());
    }
    public fun control_rollout(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, state: u8, skip: vector<ID>, reason: String, clock: &Clock, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(fleet.deployments.contains(&object::id(record)), EBinding);
        deployment_manager::control_rollout_as(record, state, skip, reason, clock, object::id(fleet).to_address());
    }

    public fun adopt_deployment(customer: &Customer, fleet: &mut Fleet, record: &mut DeploymentRecord, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert_targets(fleet, deployment_manager::target_groups(record), deployment_manager::target_devices(record));
        deployment_manager::handover(record, object::id(fleet).to_address(), ctx);
        deployment_manager::bind_customer(record, object::id(fleet));
        fleet.deployments.push_back(object::id(record));
    }
    public fun delete_deployment(customer: &Customer, fleet: &mut Fleet, mut record: DeploymentRecord, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        let (found, index) = fleet.deployments.index_of(&object::id(&record)); assert!(found, EBinding);
        fleet.deployments.swap_remove(index);
        if (deployment_manager::schema_version(&record) == 2) component_catalog::remove_for_customer(&mut record, object::id(fleet).to_address());
        deployment_manager::delete_for_customer(record, object::id(fleet).to_address());
    }
    public fun report_status(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, registry: &DeviceRegistry, cap: &DeviceCap, status: u8, ctx: &TxContext) {
        assert_reporter(customer, fleet, record, registry, cap, ctx);
        assert!(deployment_manager::target_devices(record).contains(&device_cap::device_id(cap)), EBinding);
        deployment_manager::record_customer_status(record, object::id(fleet), cap, status, ctx);
    }
    public fun report_group_status(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, registry: &DeviceRegistry, cap: &DeviceCap, group: &DeviceGroup, status: u8, ctx: &TxContext) {
        assert_reporter(customer, fleet, record, registry, cap, ctx); assert_group(fleet, group);
        assert!(deployment_manager::target_groups(record).contains(&object::id(group)) && device_group::contains_device(group, device_cap::device_id(cap)), EBinding);
        deployment_manager::record_customer_status(record, object::id(fleet), cap, status, ctx);
    }
    fun assert_reporter(customer: &Customer, fleet: &Fleet, record: &DeploymentRecord, registry: &DeviceRegistry, cap: &DeviceCap, ctx: &TxContext) {
        assert_fleet(customer, fleet);
        assert!(object::id(registry) == customer.registry_id && fleet.devices.contains(&device_cap::device_id(cap))
            && fleet.deployments.contains(&object::id(record)) && deployment_manager::creator(record) == object::id(fleet).to_address(), EBinding);
        device_cap::assert_active(cap, registry, ctx);
    }
    public fun revoke_policy_group(customer: &Customer, fleet: &Fleet, policy: &mut AccessPolicy, group_id: ID, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        let cap = dof::borrow(&fleet.id, PolicyKey { policy_id: object::id(policy) });
        seal_policy::revoke_group(policy, cap, group_id);
    }

    public fun set_control(customer: &Customer, fleet: &Fleet, record: &mut DeploymentRecord, state: u8,
        not_before_ms: u64, expires_ms: u64, window_start_ms: u64, window_end_ms: u64, notice_ms: u64, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet);
        assert!(fleet.deployments.contains(&object::id(record)), EBinding);
        deployment_manager::set_control_as(record, state, not_before_ms, expires_ms, window_start_ms, window_end_ms, notice_ms, object::id(fleet).to_address());
    }
    public fun begin_revision(customer: &Customer, fleet: &Fleet, group: &DeviceGroup, ctx: &TxContext): RevisionPlan {
        assert_admin(customer, ctx); assert_fleet(customer, fleet); assert_group(fleet, group);
        desired_state::begin_as(group, object::id(fleet).to_address())
    }
    public fun commit_revision(customer: &Customer, fleet: &Fleet, plan: RevisionPlan, group: &mut DeviceGroup, clock: &Clock, ctx: &TxContext) {
        assert_admin(customer, ctx); assert_fleet(customer, fleet); assert_group(fleet, group);
        desired_state::commit_as(plan, group, clock, object::id(fleet).to_address());
    }
    fun assert_targets(fleet: &Fleet, groups: &vector<ID>, devices: &vector<ID>) {
        let mut i = 0;
        while (i < groups.length()) { assert!(fleet.groups.contains(&groups[i]), EBinding); i = i + 1; };
        i = 0;
        while (i < devices.length()) { assert!(fleet.devices.contains(&devices[i]), EBinding); i = i + 1; };
    }

    fun assert_group(fleet: &Fleet, group: &DeviceGroup) {
        assert!(fleet.groups.contains(&object::id(group)) && device_group::admin(group) == object::id(fleet).to_address(), EBinding);
    }

    /// Reserve customer allowance before fleet diagnostics/log work. The receipt
    /// is an on-chain counter, never a reset per credential.
    public fun reserve_allowance(customer: &mut Customer, fleet: &Fleet, amount: u64, ctx: &TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert!(amount <= customer.allowance - customer.used, ELimit);
        customer.used = customer.used + amount;
    }

    /// Device writes consume the same customer ledger as administrator writes.
    /// The charge is serialized on Customer; storage validates its budget first.
    public fun reserve_device_allowance<W: drop>(customer: &mut Customer, fleet: &Fleet, budget: &FleetBudget,
        account: &GasAccount, registry: &DeviceRegistry, cap: &DeviceCap, amount: u64, _witness: W, ctx: &TxContext) {
        assert!(customer.charge_type == std::type_name::with_original_ids<W>().into_string().into_bytes(), EAccess);
        assert_fleet(customer, fleet);
        assert!(fleet.pending_devices == 0 && object::id(budget) == fleet.budget_id, EBinding);
        assert!(fleet.devices.contains(&device_cap::device_id(cap)), EBinding);
        fleet_budget::assert_may_draw(budget, account, registry, cap, ctx);
        assert!(amount <= customer.allowance - customer.used, ELimit);
        customer.used = customer.used + amount;
    }

    /// Only entitlement authority can authorize an external storage producer.
    /// A non-store, non-publicly-constructible witness then keeps devices from
    /// exhausting the customer ledger by calling its charge function directly.
    public fun configure_charging<W: drop>(customer: &mut Customer, registry: &DeviceRegistry, _witness: W, ctx: &TxContext) {
        assert!(customer.version == 1 && object::id(registry) == customer.registry_id, EBinding);
        device_registry::assert_admin(registry, ctx);
        let name = std::type_name::with_original_ids<W>().into_string().into_bytes();
        assert!(customer.charge_type.is_empty() || customer.charge_type == name, EBinding);
        customer.charge_type = name;
    }

    public fun assert_member(customer: &Customer, ctx: &TxContext) {
        assert!(customer.version == 1 && customer.credentials.contains(&ctx.sender()), EAccess);
    }
    public fun assert_admin(customer: &Customer, ctx: &TxContext) {
        assert_member(customer, ctx);
        assert!(*customer.credentials.get(&ctx.sender()), EAccess);
    }
    public fun assert_fleet(customer: &Customer, fleet: &Fleet) {
        assert!(customer.version == 1 && fleet.version == 1 && fleet.customer_id == object::id(customer) && customer.fleets.contains(&object::id(fleet)), EBinding);
    }
    fun assert_name(name: &String) { assert!(name.length() > 0 && name.length() <= 80, EName); }
    public fun devices(fleet: &Fleet): &vector<ID> { &fleet.devices }
    public fun pending_devices(fleet: &Fleet): u64 { fleet.pending_devices }
    public fun fleet_customer(fleet: &Fleet): ID { fleet.customer_id }
    public fun fleet_budget(fleet: &Fleet): ID { fleet.budget_id }
    public fun fleets(customer: &Customer): &vector<ID> { &customer.fleets }
    public fun active_devices(customer: &Customer): u64 { customer.active_devices }
    public fun used(customer: &Customer): u64 { customer.used }    #[test_only]
    public fun set_authority_version_for_testing(customer: &mut Customer, fleet: &mut Fleet, version: u64) {
        customer.version = version; fleet.version = version;
    }

}
