#[test_only]
module sui_edge::fleet_inbox_tests {
    use std::string;
    use sui::clock::{Self, Clock};
    use sui::test_scenario::{Self as ts, Scenario};
    use sui_edge::deployment_manager::{Self as dm, DeploymentRecord};
    use sui_edge::device_group::{Self, DeviceGroup};
    use sui_edge::device_registry::{Self, DeviceRegistry, DeviceOwnerCap};
    use sui_edge::fleet_inbox::{Self as inbox, DeviceInbox};

    const OPERATOR: address = @0xa;
    const STRANGER: address = @0xb;
    const KEY: address = @0xd;
    const DIGEST: vector<u8> = b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    /// Registry, one group and one activated device whose cap OPERATOR holds.
    fun setup(): (Scenario, ID, ID) {
        let mut s = ts::begin(OPERATOR);
        device_registry::init_for_testing(ts::ctx(&mut s));
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        device_group::create_group(string::utf8(b"fleet"), string::utf8(b""), &clock, ts::ctx(&mut s));
        ts::next_tx(&mut s, OPERATOR);
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let group = ts::take_shared<DeviceGroup>(&s);
        let group_id = object::id(&group);
        let device = device_registry::register_device(&mut registry, string::utf8(b"edge"), KEY,
            string::utf8(b"arm"), &clock, ts::ctx(&mut s));
        let device_id = object::id(&device);
        let cap = device_registry::activate_device(&mut registry, device, ts::ctx(&mut s));
        inbox::create_inbox(&mut registry, &cap);
        transfer::public_transfer(cap, OPERATOR);
        ts::return_shared(group);
        ts::return_shared(registry);
        clock::destroy_for_testing(clock);
        ts::next_tx(&mut s, OPERATOR);
        (s, group_id, device_id)
    }

    fun record(name: vector<u8>, groups: vector<ID>, devices: vector<ID>, clock: &Clock, s: &mut Scenario): DeploymentRecord {
        dm::new_deployment_verified(string::utf8(name), string::utf8(b"1.0.0"), string::utf8(b"blob"),
            string::utf8(b"sha"), string::utf8(DIGEST), groups, devices, clock, ts::ctx(s))
    }

    /// Adds a device to the group through the custody loan, as the dApp does.
    fun add_member(s: &mut Scenario, with_inbox: bool) {
        let mut registry = ts::take_shared<DeviceRegistry>(s);
        let mut group = ts::take_shared<DeviceGroup>(s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(s);
        let cap = ts::take_from_sender<DeviceOwnerCap>(s);
        let (device, loan) = device_registry::borrow_device(&mut registry, &cap);
        if (with_inbox) {
            inbox::add_member(&mut group, &mut device_inbox, &device, ts::ctx(s));
        } else {
            device_group::add_device(&mut group, &device, ts::ctx(s));
        };
        device_registry::return_device(&mut registry, device, loan);
        ts::return_to_sender(s, cap);
        ts::return_shared(device_inbox);
        ts::return_shared(group);
        ts::return_shared(registry);
    }

    #[test]
    fun inbox_lives_at_its_derived_address() {
        let (mut s, _, device_id) = setup();
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let device_inbox = ts::take_shared<DeviceInbox>(&s);
        assert!(inbox::inbox_exists(&registry, device_id), 0);
        assert!(object::id(&device_inbox).to_address() == inbox::inbox_address(object::id(&registry), device_id), 1);
        assert!(inbox::inbox_device_id(&device_inbox) == device_id, 2);
        let head = inbox::inbox_head(&device_inbox);
        assert!(inbox::seq(&head) == 0 && inbox::latest(&head).is_none(), 3);
        ts::return_shared(device_inbox);
        ts::return_shared(registry);
        ts::end(s);
    }

    #[test]
    fun a_new_inbox_takes_announcements_before_it_is_shared() {
        let mut s = ts::begin(OPERATOR);
        device_registry::init_for_testing(ts::ctx(&mut s));
        ts::next_tx(&mut s, OPERATOR);
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let device = device_registry::register_device(&mut registry, string::utf8(b"edge"), KEY,
            string::utf8(b"arm"), &clock, ts::ctx(&mut s));
        let device_id = object::id(&device);
        let cap = device_registry::activate_device(&mut registry, device, ts::ctx(&mut s));
        let d = record(b"app", vector[], vector[device_id], &clock, &mut s);
        let mut device_inbox = inbox::new_inbox(&mut registry, &cap);
        inbox::announce_device(&mut device_inbox, &registry, &cap, &d, &clock, ts::ctx(&mut s));
        assert!(inbox::seq(&inbox::inbox_head(&device_inbox)) == 1, 0);
        inbox::share_inbox(device_inbox);
        dm::share_deployment(d);
        transfer::public_transfer(cap, OPERATOR);
        ts::return_shared(registry);
        clock::destroy_for_testing(clock);
        ts::end(s);
    }

    #[test]
    #[expected_failure]
    fun inbox_is_created_once() {
        let (mut s, _, _) = setup();
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        inbox::create_inbox(&mut registry, &cap);
        abort 99
    }

    #[test]
    fun operator_announces_to_device_and_group() {
        let (mut s, group_id, device_id) = setup();
        add_member(&mut s, true);
        ts::next_tx(&mut s, OPERATOR);
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        let mut clock = clock::create_for_testing(ts::ctx(&mut s));
        clock.set_for_testing(1_000);
        assert!(inbox::inbox_groups(&device_inbox) == &vector[group_id], 0);

        let first = record(b"app", vector[group_id], vector[device_id], &clock, &mut s);
        inbox::announce_group(&mut group, &first, &clock, ts::ctx(&mut s));
        inbox::announce_device(&mut device_inbox, &registry, &cap, &first, &clock, ts::ctx(&mut s));
        // A later scheduled deployment of the same component replaces it and
        // arms the next start; an earlier one moves the start forward.
        let mut later = record(b"app", vector[group_id], vector[], &clock, &mut s);
        dm::set_control(&mut later, 0, 9_000, 0, 0, 0, 0, ts::ctx(&mut s));
        inbox::announce_group(&mut group, &later, &clock, ts::ctx(&mut s));
        let mut sooner = record(b"other", vector[group_id], vector[], &clock, &mut s);
        dm::set_control(&mut sooner, 0, 5_000, 0, 0, 0, 0, ts::ctx(&mut s));
        inbox::announce_group(&mut group, &sooner, &clock, ts::ctx(&mut s));
        let mut farther = record(b"third", vector[group_id], vector[], &clock, &mut s);
        dm::set_control(&mut farther, 0, 7_000, 0, 0, 0, 0, ts::ctx(&mut s));
        inbox::announce_group(&mut group, &farther, &clock, ts::ctx(&mut s));

        let head = inbox::group_head(&group);
        assert!(inbox::seq(&head) == 4, 1);
        assert!(inbox::latest(&head) == option::some(object::id(&farther)), 2);
        assert!(inbox::next_start_ms(&head) == 5_000, 3);
        let components = inbox::components(&head);
        assert!(components.length() == 3, 4);
        assert!(*components.get(&string::utf8(b"app")) == object::id(&later), 5);
        let device_head = inbox::inbox_head(&device_inbox);
        assert!(inbox::seq(&device_head) == 1 && inbox::latest(&device_head) == option::some(object::id(&first)), 6);
        assert!(inbox::next_start_ms(&device_head) == 0, 7);

        // Once the earliest start passed, the next future start replaces it.
        clock.set_for_testing(6_000);
        inbox::announce_group(&mut group, &later, &clock, ts::ctx(&mut s));
        assert!(inbox::next_start_ms(&inbox::group_head(&group)) == 9_000, 8);

        dm::share_deployment(first); dm::share_deployment(later);
        dm::share_deployment(sooner); dm::share_deployment(farther);
        ts::return_to_sender(&s, cap);
        ts::return_shared(device_inbox);
        ts::return_shared(group);
        ts::return_shared(registry);
        clock::destroy_for_testing(clock);
        ts::end(s);
    }

    #[test]
    fun heads_remember_a_bounded_set_of_components() {
        let (mut s, group_id, _) = setup();
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let names = b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+-#";
        let mut i = 0;
        while (i < names.length()) {
            let d = record(vector[names[i]], vector[group_id], vector[], &clock, &mut s);
            inbox::announce_group(&mut group, &d, &clock, ts::ctx(&mut s));
            dm::share_deployment(d);
            i = i + 1;
        };
        let components = inbox::components(&inbox::group_head(&group));
        assert!(components.length() == 64, 0);
        assert!(!components.contains(&string::utf8(b"0")), 1);
        assert!(components.contains(&string::utf8(b"#")), 2);
        ts::return_shared(group);
        clock::destroy_for_testing(clock);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotCreator)]
    fun only_the_creator_announces_to_a_group() {
        let (mut s, group_id, _) = setup();
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let d = record(b"app", vector[group_id], vector[], &clock, &mut s);
        dm::share_deployment(d);
        ts::next_tx(&mut s, STRANGER);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let d = ts::take_shared<DeploymentRecord>(&s);
        inbox::announce_group(&mut group, &d, &clock, ts::ctx(&mut s));
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotAdmin)]
    fun a_foreign_creator_cannot_announce_to_a_group() {
        let (mut s, group_id, _) = setup();
        ts::next_tx(&mut s, STRANGER);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let d = record(b"app", vector[group_id], vector[], &clock, &mut s);
        inbox::announce_group(&mut group, &d, &clock, ts::ctx(&mut s));
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotTargeted)]
    fun an_untargeted_group_cannot_be_announced() {
        let (mut s, _, device_id) = setup();
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let d = record(b"app", vector[], vector[device_id], &clock, &mut s);
        inbox::announce_group(&mut group, &d, &clock, ts::ctx(&mut s));
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotTargeted)]
    fun an_untargeted_device_cannot_be_announced() {
        let (mut s, group_id, _) = setup();
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let d = record(b"app", vector[group_id], vector[], &clock, &mut s);
        inbox::announce_device(&mut device_inbox, &registry, &cap, &d, &clock, ts::ctx(&mut s));
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotCreator)]
    fun only_the_creator_announces_to_a_device() {
        let (mut s, _, device_id) = setup();
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let d = record(b"app", vector[], vector[device_id], &clock, &mut s);
        dm::share_deployment(d);
        // The operator hands its cap to a stranger, who did not create the record.
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        transfer::public_transfer(cap, STRANGER);
        ts::next_tx(&mut s, STRANGER);
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        let d = ts::take_shared<DeploymentRecord>(&s);
        inbox::announce_device(&mut device_inbox, &registry, &cap, &d, &clock, ts::ctx(&mut s));
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotOperator)]
    fun a_device_out_of_custody_takes_no_announcements() {
        let (mut s, _, device_id) = setup();
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        let cap = ts::take_from_sender<DeviceOwnerCap>(&s);
        let (device, loan) = device_registry::borrow_device(&mut registry, &cap);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let d = record(b"app", vector[], vector[device_id], &clock, &mut s);
        inbox::announce_device(&mut device_inbox, &registry, &cap, &d, &clock, ts::ctx(&mut s));
        device_registry::return_device(&mut registry, device, loan);
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::EWrongInbox)]
    fun another_devices_cap_does_not_open_an_inbox() {
        let (mut s, _, device_id) = setup();
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let other = device_registry::register_device(&mut registry, string::utf8(b"other"), STRANGER,
            string::utf8(b"arm"), &clock, ts::ctx(&mut s));
        let other_cap = device_registry::activate_device(&mut registry, other, ts::ctx(&mut s));
        let d = record(b"app", vector[], vector[device_id], &clock, &mut s);
        inbox::announce_device(&mut device_inbox, &registry, &other_cap, &d, &clock, ts::ctx(&mut s));
        abort 99
    }

    #[test]
    fun membership_moves_into_the_index() {
        let (mut s, group_id, device_id) = setup();
        add_member(&mut s, false);
        ts::next_tx(&mut s, OPERATOR);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let other = object::id_from_address(@0x77);
        device_group::legacy_add_for_testing(&mut group, other);
        assert!(!device_group::is_indexed(&group) && device_group::member_count(&group) == 2, 0);
        device_group::index_members(&mut group, 1, ts::ctx(&mut s));
        assert!(device_group::is_indexed(&group) && device_group::device_ids(&group).length() == 1, 1);
        device_group::index_members(&mut group, 5, ts::ctx(&mut s));
        assert!(device_group::device_ids(&group).is_empty(), 2);
        assert!(device_group::member_count(&group) == 2, 3);
        assert!(device_group::contains_device(&group, device_id) && device_group::contains_device(&group, other), 4);
        // An older package can still re-add an indexed member to the vector;
        // removal clears both, and indexing drops the duplicate.
        device_group::legacy_add_for_testing(&mut group, other);
        device_group::index_members(&mut group, 5, ts::ctx(&mut s));
        assert!(device_group::member_count(&group) == 2, 5);
        device_group::legacy_add_for_testing(&mut group, other);
        device_group::remove_device(&mut group, other, ts::ctx(&mut s));
        assert!(!device_group::contains_device(&group, other) && device_group::member_count(&group) == 1, 6);
        ts::return_shared(group);

        // A device added once indexed stays out of the vector, and the
        // inbox can follow a group it was added to earlier.
        ts::next_tx(&mut s, OPERATOR);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        inbox::follow_group(&mut device_inbox, &group);
        assert!(inbox::inbox_groups(&device_inbox) == &vector[group_id], 7);
        inbox::remove_member(&mut group, &mut device_inbox, ts::ctx(&mut s));
        assert!(inbox::inbox_groups(&device_inbox).is_empty() && device_group::member_count(&group) == 0, 8);
        ts::return_shared(device_inbox);
        ts::return_shared(group);
        ts::next_tx(&mut s, OPERATOR);
        add_member(&mut s, true);
        ts::next_tx(&mut s, OPERATOR);
        let group = ts::take_shared<DeviceGroup>(&s);
        assert!(device_group::device_ids(&group).is_empty() && device_group::contains_device(&group, device_id), 9);
        ts::return_shared(group);
        ts::end(s);
    }

    #[test]
    #[expected_failure(abort_code = device_group::EDeviceAlreadyInGroup)]
    fun an_indexed_member_cannot_be_added_twice() {
        let (mut s, _, _) = setup();
        let mut group = ts::take_shared<DeviceGroup>(&s);
        device_group::index_members(&mut group, 1, ts::ctx(&mut s));
        ts::return_shared(group);
        ts::next_tx(&mut s, OPERATOR);
        add_member(&mut s, true);
        ts::next_tx(&mut s, OPERATOR);
        add_member(&mut s, false);
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = device_group::ENotAdmin)]
    fun only_the_admin_indexes_a_group() {
        let (mut s, _, _) = setup();
        ts::next_tx(&mut s, STRANGER);
        let mut group = ts::take_shared<DeviceGroup>(&s);
        device_group::index_members(&mut group, 1, ts::ctx(&mut s));
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotMember)]
    fun a_non_member_cannot_follow_a_group() {
        let (mut s, _, _) = setup();
        let mut group = ts::take_shared<DeviceGroup>(&s);
        device_group::index_members(&mut group, 1, ts::ctx(&mut s));
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        inbox::follow_group(&mut device_inbox, &group);
        abort 99
    }

    #[test]
    #[expected_failure(abort_code = inbox::ENotOperator)]
    fun only_the_operator_forgets_a_group() {
        let (mut s, group_id, _) = setup();
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let mut device_inbox = ts::take_shared<DeviceInbox>(&s);
        // A cap for the same device that the identity does not name.
        let forged = device_registry::owner_cap_for_testing(inbox::inbox_device_id(&device_inbox), ts::ctx(&mut s));
        inbox::forget_group(&mut device_inbox, &registry, &forged, group_id);
        abort 99
    }

    #[test]
    fun indexed_membership_authorizes_group_reports() {
        let (mut s, group_id, device_id) = setup();
        let mut group = ts::take_shared<DeviceGroup>(&s);
        device_group::index_members(&mut group, 1, ts::ctx(&mut s));
        ts::return_shared(group);
        ts::next_tx(&mut s, OPERATOR);
        add_member(&mut s, true);
        ts::next_tx(&mut s, OPERATOR);
        let mut registry = ts::take_shared<DeviceRegistry>(&s);
        let owner = ts::take_from_sender<DeviceOwnerCap>(&s);
        let (device, loan) = device_registry::borrow_device(&mut registry, &owner);
        sui_edge::device_cap::issue_cap(&device, ts::ctx(&mut s));
        device_registry::return_device(&mut registry, device, loan);
        ts::return_to_sender(&s, owner);
        let clock = clock::create_for_testing(ts::ctx(&mut s));
        let d = record(b"app", vector[group_id], vector[], &clock, &mut s);
        dm::share_deployment(d);
        ts::return_shared(registry);
        ts::next_tx(&mut s, KEY);
        let registry = ts::take_shared<DeviceRegistry>(&s);
        let group = ts::take_shared<DeviceGroup>(&s);
        let mut d = ts::take_shared<DeploymentRecord>(&s);
        let cap = ts::take_from_sender<sui_edge::device_cap::DeviceCap>(&s);
        dm::report_verified_status_for_group(&mut d, &registry, &cap, &group, dm::status_applied(), ts::ctx(&mut s));
        assert!(dm::receipt_status(&d, device_id) == option::some(dm::status_applied()), 0);
        ts::return_to_sender(&s, cap);
        ts::return_shared(d);
        ts::return_shared(group);
        ts::return_shared(registry);
        clock::destroy_for_testing(clock);
        ts::end(s);
    }
}
