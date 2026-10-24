/// Desired fleet state (SF-013): what each device group should be running.
///
/// A group's `DesiredState` maps component names to the DeploymentRecord
/// each member should have applied. Changing it is one revision: a
/// `RevisionPlan` is begun, edited and committed in a single transaction (it
/// has no abilities, so it cannot outlive that transaction), so every
/// component change in a revision becomes desired together or not at all.
///
/// Devices reconcile toward the latest revision of every group they belong
/// to: a superseded deployment is never applied, a device that joins a group
/// receives its current desired state, and one that leaves stops following it.
/// Records referenced by a revision are marked managed, so devices apply them
/// through reconciliation rather than as one-shot creation events.
///
/// Introduced by a package upgrade: older package versions cannot read or
/// write any of these types.
module sui_edge::desired_state {
    use std::string::String;
    use sui::clock::Clock;
    use sui::dynamic_field as df;
    use sui::event;
    use sui::vec_map::{Self, VecMap};
    use sui_edge::component_catalog;
    use sui_edge::deployment_manager::{Self, DeploymentRecord};
    use sui_edge::device_group::{Self, DeviceGroup};

    // ── Errors ────────────────────────────────────────────────────────────────

    const ENotAdmin: u64 = 0;
    const ENotCreator: u64 = 1;
    const ENotTargeted: u64 = 2;
    const EUnverifiedRecipe: u64 = 3;
    const EWrongGroup: u64 = 4;
    const EStaleRevision: u64 = 5;
    const EUnknownComponent: u64 = 6;
    const EStillDesired: u64 = 7;
    const EUnconfirmed: u64 = 8;
    const EWrongRecord: u64 = 9;
    const ERetiring: u64 = 10;

    // ── State ─────────────────────────────────────────────────────────────────

    /// Dynamic-field key on a DeviceGroup.
    public struct DesiredKey has copy, drop, store {}

    public struct DesiredState has store, drop {
        /// 1 for the first committed revision; +1 per commit.
        revision: u64,
        /// Component name → DeploymentRecord ID.
        components: VecMap<String, ID>,
        updated_at: u64,
    }

    /// Dynamic-field key on a DeviceGroup (SF-129): records confirmed not
    /// desired by `confirm_not_desired` in the transaction `tx`. `commit` in
    /// that transaction refuses to make them desired again; a later commit
    /// removes the stale check and its deposit.
    public struct RetirementCheckKey has copy, drop, store {}

    public struct RetirementCheck has store, drop {
        tx: vector<u8>,
        deployments: vector<ID>,
    }

    /// An uncommitted revision of one group's desired state.
    public struct RevisionPlan {
        group_id: ID,
        admin: address,
        base_revision: u64,
        components: VecMap<String, ID>,
    }

    public struct DesiredStateChanged has copy, drop {
        group_id: ID,
        revision: u64,
        component_names: vector<String>,
        deployments: vector<ID>,
    }

    // ── Revisions ─────────────────────────────────────────────────────────────

    /// Start a revision from the group's current desired state. Admin only.
    public fun begin(group: &DeviceGroup, ctx: &TxContext): RevisionPlan {
        let admin = device_group::admin(group);
        assert!(ctx.sender() == admin, ENotAdmin);
        let (base_revision, components) = if (df::exists(device_group::uid(group), DesiredKey {})) {
            let state: &DesiredState = df::borrow(device_group::uid(group), DesiredKey {});
            (state.revision, state.components)
        } else {
            (0, vec_map::empty())
        };
        RevisionPlan { group_id: device_group::group_id(group), admin, base_revision, components }
    }

    /// Make `deployment` the desired version of its component. The record must
    /// be the admin's own, target this group and authenticate its recipe.
    public fun set_component(plan: &mut RevisionPlan, deployment: &mut DeploymentRecord) {
        assert!(deployment_manager::creator(deployment) == plan.admin, ENotCreator);
        assert!(deployment_manager::target_groups(deployment).contains(&plan.group_id), ENotTargeted);
        assert!(deployment_manager::has_recipe_digest(deployment), EUnverifiedRecipe);
        deployment_manager::mark_managed(deployment);
        let name = *deployment_manager::component_name(deployment);
        let id = object::id(deployment);
        if (plan.components.contains(&name)) {
            *plan.components.get_mut(&name) = id;
        } else {
            plan.components.insert(name, id);
        };
    }

    /// Stop desiring a component. Devices retire it from management.
    public fun remove_component(plan: &mut RevisionPlan, name: String) {
        assert!(plan.components.contains(&name), EUnknownComponent);
        plan.components.remove(&name);
    }

    /// Store the revision on the group. Aborts if the group changed since `begin`.
    public fun commit(plan: RevisionPlan, group: &mut DeviceGroup, clock: &Clock, ctx: &TxContext) {
        let RevisionPlan { group_id, admin, base_revision, components } = plan;
        assert!(device_group::group_id(group) == group_id, EWrongGroup);
        assert!(ctx.sender() == admin && device_group::admin(group) == admin, ENotAdmin);
        assert!(revision(group) == base_revision, EStaleRevision);
        let state = DesiredState { revision: base_revision + 1, components, updated_at: clock.timestamp_ms() };
        let (component_names, deployments) = state.components.into_keys_values();
        let uid = device_group::uid_mut(group);
        if (df::exists(uid, RetirementCheckKey {})) {
            let check: &RetirementCheck = df::borrow(uid, RetirementCheckKey {});
            if (check.tx == *ctx.digest()) {
                assert!(!deployments.any!(|id| check.deployments.contains(id)), ERetiring);
            } else {
                let _: RetirementCheck = df::remove(uid, RetirementCheckKey {});
            };
        };
        event::emit(DesiredStateChanged { group_id, revision: state.revision, component_names, deployments });
        if (df::exists(uid, DesiredKey {})) {
            let _: DesiredState = df::remove(uid, DesiredKey {});
        };
        df::add(uid, DesiredKey {}, state);
    }

    // ── Retirement (SF-129) ───────────────────────────────────────────────────

    /// Proof, within one transaction, that no group still desires a record.
    /// No abilities: it must be consumed by `retire` in the same transaction.
    ///
    /// The proof stays valid for the rest of the transaction: a confirmed
    /// group's `commit` refuses the record (`ERetiring`), and `retire` refuses
    /// a record that became managed after `begin_retirement`. A plan prepared
    /// earlier in the transaction is covered by the same `commit` check.
    public struct Retirement {
        deployment_id: ID,
        /// Whether the record was managed at `begin_retirement`.
        managed: bool,
        /// Target groups whose current desired state is still unchecked.
        pending: vector<ID>,
    }

    /// Start deleting a finished record. Only a managed record can be desired,
    /// and only by a group it targets (`set_component`), so those groups are
    /// the complete set to check. Creator only.
    public fun begin_retirement(deployment: &DeploymentRecord, ctx: &mut TxContext): Retirement {
        assert!(ctx.sender() == deployment_manager::creator(deployment), ENotCreator);
        let managed = deployment_manager::is_managed(deployment);
        let pending = if (managed) *deployment_manager::target_groups(deployment) else vector[];
        Retirement { deployment_id: object::id(deployment), managed, pending }
    }

    /// Check one target group's current desired state. Aborts while it still
    /// references the record: commit a revision that supersedes it first.
    /// Records the check on the group so a later `commit` in this transaction
    /// cannot make the record desired again.
    public fun confirm_not_desired(retirement: &mut Retirement, group: &mut DeviceGroup, ctx: &TxContext) {
        let group_id = device_group::group_id(group);
        assert!(retirement.pending.contains(&group_id), EWrongGroup);
        let (_, deployments) = components(group).into_keys_values();
        assert!(!deployments.contains(&retirement.deployment_id), EStillDesired);
        let uid = device_group::uid_mut(group);
        if (df::exists(uid, RetirementCheckKey {})) {
            let check: &mut RetirementCheck = df::borrow_mut(uid, RetirementCheckKey {});
            if (check.tx != *ctx.digest()) {
                check.tx = *ctx.digest();
                check.deployments = vector[];
            };
            check.deployments.push_back(retirement.deployment_id);
        } else {
            df::add(uid, RetirementCheckKey {}, RetirementCheck { tx: *ctx.digest(), deployments: vector[retirement.deployment_id] });
        };
        // A record may list a group more than once.
        while (retirement.pending.contains(&group_id)) {
            let (_, index) = retirement.pending.index_of(&group_id);
            retirement.pending.swap_remove(index);
        };
    }

    /// Delete the record and return its storage rebate once every target group
    /// is confirmed. Catalog bindings are removed by their defining module
    /// first; devices already refuse deleted records.
    public fun retire(retirement: Retirement, deployment: DeploymentRecord, ctx: &mut TxContext) {
        let Retirement { deployment_id, managed, pending } = retirement;
        assert!(object::id(&deployment) == deployment_id, EWrongRecord);
        assert!(pending.is_empty(), EUnconfirmed);
        // Assigned to a plan after `begin_retirement`: its groups were never checked.
        assert!(managed || !deployment_manager::is_managed(&deployment), ERetiring);
        let schema = deployment_manager::schema_version(&deployment);
        if (schema == 2) component_catalog::delete_deployment(deployment, ctx)
        else if (schema == 3) deployment_manager::delete_rollout(deployment, ctx)
        else deployment_manager::delete_deployment(deployment, ctx);
    }

    public fun retirement_pending(retirement: &Retirement): &vector<ID> { &retirement.pending }

    // ── Reads ─────────────────────────────────────────────────────────────────

    /// 0 until the first commit.
    public fun revision(group: &DeviceGroup): u64 {
        if (df::exists(device_group::uid(group), DesiredKey {})) {
            let state: &DesiredState = df::borrow(device_group::uid(group), DesiredKey {});
            state.revision
        } else { 0 }
    }

    /// Empty until the first commit.
    public fun components(group: &DeviceGroup): VecMap<String, ID> {
        if (df::exists(device_group::uid(group), DesiredKey {})) {
            let state: &DesiredState = df::borrow(device_group::uid(group), DesiredKey {});
            state.components
        } else { vec_map::empty() }
    }

    public fun plan_components(plan: &RevisionPlan): &VecMap<String, ID> { &plan.components }
}
