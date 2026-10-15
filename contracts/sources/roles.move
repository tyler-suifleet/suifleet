/// Scoped deployer roles (SF-021): an operator lets a delegate deploy to
/// specific groups on its behalf, revocably.
///
/// A `DeployerRole` is built in one transaction: `new_deployer_role`, then
/// `allow_group` for each group (the operator must administer it; group
/// admins never change), then `grant` to the delegate. The role is active
/// while its entry exists on the shared registry, so the operator can
/// `revoke` it without holding it.
///
/// `create_deployment_as` creates a verified group deployment whose
/// `creator` is the operator, so devices accept it exactly like the
/// operator's own, with the delegate recorded for audit. Delegates cannot
/// target devices directly, use other groups, or delete records.
///
/// Introduced by a package upgrade: older package versions cannot reach it.
module sui_edge::roles {
    use std::string::String;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui_edge::deployment_manager;
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::device_registry::{Self, DeviceRegistry};

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENotGroupAdmin: u64 = 0;
    const ENoGroups: u64 = 1;
    const ERoleRevoked: u64 = 2;
    const EOutsideScope: u64 = 3;
    const ENotOperator: u64 = 4;
    const ENoTargets: u64 = 5;

    // ── State ─────────────────────────────────────────────────────────────────

    public struct DeployerRole has key, store {
        id: UID,
        operator: address,
        groups: vector<ID>,
    }

    /// Unfinished role (no abilities): finished by `grant` in the same transaction.
    public struct RoleBuilder {
        operator: address,
        groups: vector<ID>,
    }

    /// Registry dynamic field: present while the role is active; value = operator.
    public struct RoleKey has copy, drop, store { role_id: ID }

    public struct RoleGranted has copy, drop {
        role_id: ID,
        operator: address,
        delegate: address,
        groups: vector<ID>,
    }

    public struct RoleRevoked has copy, drop {
        role_id: ID,
        operator: address,
    }

    public struct DelegatedDeployment has copy, drop {
        deployment_id: ID,
        role_id: ID,
        operator: address,
        delegate: address,
    }

    // ── Operator ──────────────────────────────────────────────────────────────

    #[allow(lint(prefer_mut_tx_context))]
    public fun new_deployer_role(ctx: &TxContext): RoleBuilder {
        RoleBuilder { operator: ctx.sender(), groups: vector[] }
    }

    public fun allow_group(builder: &mut RoleBuilder, group: &DeviceGroup) {
        assert!(device_group::admin(group) == builder.operator, ENotGroupAdmin);
        let id = device_group::group_id(group);
        if (!builder.groups.contains(&id)) {
            builder.groups.push_back(id);
        };
    }

    public fun grant(builder: RoleBuilder, registry: &mut DeviceRegistry, delegate: address, ctx: &mut TxContext) {
        let RoleBuilder { operator, groups } = builder;
        assert!(ctx.sender() == operator, ENotOperator);
        assert!(!groups.is_empty(), ENoGroups);
        let role = DeployerRole { id: object::new(ctx), operator, groups };
        let role_id = object::id(&role);
        df::add(device_registry::uid_mut(registry), RoleKey { role_id }, operator);
        event::emit(RoleGranted { role_id, operator, delegate, groups: role.groups });
        transfer::public_transfer(role, delegate);
    }

    /// Deactivate a role; it can never be reactivated.
    #[allow(lint(prefer_mut_tx_context))]
    public fun revoke(registry: &mut DeviceRegistry, role_id: ID, ctx: &TxContext) {
        let key = RoleKey { role_id };
        assert!(df::exists(device_registry::uid(registry), key), ERoleRevoked);
        let operator: address = *df::borrow(device_registry::uid(registry), key);
        assert!(ctx.sender() == operator, ENotOperator);
        let _: address = df::remove(device_registry::uid_mut(registry), key);
        event::emit(RoleRevoked { role_id, operator });
    }

    // ── Delegate ──────────────────────────────────────────────────────────────

    /// A verified deployment to some of the role's groups, attributed to its
    /// operator. Holding the role (it is an owned object) authorizes the sender.
    public fun create_deployment_as(
        role: &DeployerRole,
        registry: &DeviceRegistry,
        component_name: String,
        version: String,
        recipe_blob_id: String,
        artifact_sha256: String,
        recipe_sha256: String,
        target_groups: vector<ID>,
        clock: &Clock,
        ctx: &mut TxContext,
    ) {
        assert!(is_active(registry, role), ERoleRevoked);
        assert!(!target_groups.is_empty(), ENoTargets);
        let mut i = 0;
        while (i < target_groups.length()) {
            assert!(role.groups.contains(&target_groups[i]), EOutsideScope);
            i = i + 1;
        };
        let deployment_id = deployment_manager::create_delegated(role.operator, component_name, version,
            recipe_blob_id, artifact_sha256, recipe_sha256, target_groups, clock, ctx);
        event::emit(DelegatedDeployment { deployment_id, role_id: object::id(role), operator: role.operator, delegate: ctx.sender() });
    }

    // ── Reads ─────────────────────────────────────────────────────────────────

    public fun is_active(registry: &DeviceRegistry, role: &DeployerRole): bool {
        let key = RoleKey { role_id: object::id(role) };
        df::exists(device_registry::uid(registry), key)
            && *df::borrow<RoleKey, address>(device_registry::uid(registry), key) == role.operator
    }
    public fun role_operator(role: &DeployerRole): address { role.operator }
    public fun role_groups(role: &DeployerRole): &vector<ID> { &role.groups }
}
