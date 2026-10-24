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
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::deployment_manager;
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
    }
    public struct Fleet has key {
        id: UID,
        customer_id: ID,
        name: String,
        budget_id: ID,
        devices: vector<ID>,
        groups: vector<ID>,
        deployments: vector<ID>,
    }
    /// No store: credentials cannot be transferred outside this module.
    public struct Membership has key { id: UID, customer_id: ID }
    public struct CustomerKey has copy, drop, store { wallet: address }
    public struct BudgetKey has copy, drop, store {}
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
        let mut customer = Customer { id: object::new(ctx), registry_id: object::id(registry), name,
            credentials, administrators: 1, fleets: vector[], default_fleet: option::none(),
            active_devices: 0, device_limit: 0, allowance: 0, used: 0, period: 0 };
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
        assert!(customer.registry_id == object::id(registry), EBinding);
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

    /// Adopt an EMPTY budget by consuming its exact owner cap. Nonempty legacy
    /// fleets require explicit drain/reconciliation; they are never silently adopted.
    /// The first adopted fleet is the default. Retrying with an already consumed
    /// cap cannot duplicate it; callers recover from FleetCreated/customer inventory.
    public fun adopt_budget(customer: &mut Customer, budget: &mut FleetBudget,
        cap: BudgetOwnerCap, name: String, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert_name(&name);
        assert!(fleet_budget::registry_id(budget) == customer.registry_id, EBinding);
        fleet_budget::adopt_managed(budget, &cap);
        let mut fleet = Fleet { id: object::new(ctx), customer_id: object::id(customer), name,
            budget_id: object::id(budget), devices: vector[], groups: vector[], deployments: vector[] };
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
        let cap = dof::remove(&mut fleet.id, BudgetKey {});
        let loan = BudgetLoan { fleet_id: object::id(fleet), cap_id: object::id(&cap) };
        (cap, loan)
    }
    public fun return_budget_cap(fleet: &mut Fleet, cap: BudgetOwnerCap, loan: BudgetLoan) {
        let BudgetLoan { fleet_id, cap_id } = loan;
        assert!(object::id(fleet) == fleet_id && object::id(&cap) == cap_id, EBinding);
        dof::add(&mut fleet.id, BudgetKey {}, cap);
    }

    public fun enroll(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        registry: &DeviceRegistry, cap: DeviceOwnerCap, float: u64, reserve: u64,
        clock: &Clock, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        assert!(object::id(budget) == fleet.budget_id && object::id(registry) == customer.registry_id, EBinding);
        let device_id = device_registry::owner_cap_device_id(&cap);
        assert!(!df::exists(&customer.id, AssignmentKey { device_id }), EAssigned);
        assert!(customer.active_devices < customer.device_limit, ELimit);
        let budget_cap = dof::borrow(&fleet.id, BudgetKey {});
        fleet_budget::enroll_managed(budget, budget_cap, registry, &cap, float, reserve, clock, ctx);
        df::add(&mut customer.id, AssignmentKey { device_id }, object::id(fleet));
        dof::add(&mut fleet.id, DeviceKey { device_id }, cap);
        df::add(&mut fleet.id, GroupCountKey { device_id }, 0u64);
        fleet.devices.push_back(device_id);
        customer.active_devices = customer.active_devices + 1;
        event::emit(DeviceAssigned { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
    }

    /// Explicit departure retires the gas account and releases capacity once.
    /// Clear group memberships first; historical deployments/archives stay put.
    public fun remove_device(customer: &mut Customer, fleet: &mut Fleet, budget: &mut FleetBudget,
        account: &mut GasAccount, device_id: ID, ctx: &TxContext) {
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
        transfer::public_transfer(owner_cap, ctx.sender());
        event::emit(DeviceReleased { customer_id: object::id(customer), fleet_id: object::id(fleet), device_id });
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

    public fun create_deployment(customer: &Customer, fleet: &mut Fleet,
        component_name: String, version: String, recipe_blob_id: String,
        artifact_sha256: String, recipe_sha256: String,
        groups: vector<ID>, devices: vector<ID>, clock: &Clock, ctx: &mut TxContext) {
        assert_admin(customer, ctx);
        assert_fleet(customer, fleet);
        let mut i = 0;
        while (i < groups.length()) { assert!(fleet.groups.contains(&groups[i]), EBinding); i = i + 1; };
        i = 0;
        while (i < devices.length()) { assert!(fleet.devices.contains(&devices[i]), EBinding); i = i + 1; };
        let record = deployment_manager::new_customer_deployment(object::id(fleet).to_address(),
            component_name, version, recipe_blob_id, artifact_sha256, recipe_sha256, groups, devices, clock, ctx);
        fleet.deployments.push_back(object::id(&record));
        deployment_manager::share_deployment(record);
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

    public fun assert_member(customer: &Customer, ctx: &TxContext) {
        assert!(customer.credentials.contains(&ctx.sender()), EAccess);
    }
    public fun assert_admin(customer: &Customer, ctx: &TxContext) {
        assert_member(customer, ctx);
        assert!(*customer.credentials.get(&ctx.sender()), EAccess);
    }
    public fun assert_fleet(customer: &Customer, fleet: &Fleet) {
        assert!(fleet.customer_id == object::id(customer) && customer.fleets.contains(&object::id(fleet)), EBinding);
    }
    fun assert_name(name: &String) { assert!(name.length() > 0 && name.length() <= 80, EName); }
    public fun fleet_customer(fleet: &Fleet): ID { fleet.customer_id }
    public fun fleet_budget(fleet: &Fleet): ID { fleet.budget_id }
    public fun fleets(customer: &Customer): &vector<ID> { &customer.fleets }
    public fun active_devices(customer: &Customer): u64 { customer.active_devices }
    public fun used(customer: &Customer): u64 { customer.used }
}
