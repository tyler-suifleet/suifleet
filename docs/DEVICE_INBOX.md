# Device inboxes, group heads and the member index (SF-125)

Devices read their current state from objects instead of reconstructing it
from event history. That lets a fleet run on a node that keeps only a short
event history (Mysten's public nodes keep about 14 days and answer older
ranges with `OUT_OF_RANGE`), removes package-wide scans across tenants, and
lifts the group size limit of the `device_ids` vector (about 7,800 members in
the 256 KB object limit).

This is an additive package upgrade. Existing public signatures, layouts and
abilities are unchanged; every new type is introduced by the upgrade, so older
package versions can neither read nor write it.

## Objects

| Object | Where | Contents |
|---|---|---|
| `fleet_inbox::DeviceInbox` | One shared object per device, at the address `sui::derived_object` derives from the registry and `InboxKey { device_id }` | `head` (below) and `groups`, the groups the device was added to through the inbox |
| `fleet_inbox::Head` | `head` of each inbox; a `HeadKey` dynamic field on each group | `seq` (+1 per announcement), `latest`, `next_start_ms` (earliest announced future scheduled start, 0 for none) and `components` (component name → latest announced deployment, at most 64; the oldest entry is evicted beyond that) |
| `device_group::MemberKey { device_id }` | Dynamic field (`true`) on an indexed group, per member | Membership |
| `device_group::IndexKey` → `GroupIndex` | Dynamic field on an indexed group | Number of indexed members |

Clients compute the inbox address without a lookup: the dynamic-field ID of
the registry with key type `0x2::derived_object::DerivedObjectKey<InboxKey>`
and the device ID's 32 bytes as key (`fleet_inbox::inbox_address`, the
SDK's `deriveObjectID`, `SuiClient::inbox_id`).

## Authority

- `create_inbox`/`new_inbox`: the device's operator only, proven by the
  DeviceOwnerCap the registry identity names, while the device is in custody.
  Once per device. `new_inbox` returns the inbox so the same transaction can
  announce or list a group before `share_inbox`.
- `announce_group(group, record)`: the record's creator only, who must be the
  group's admin, and only for a group the record targets.
- `announce_device(inbox, registry, cap, record)`: the record's creator only,
  holding the device's current DeviceOwnerCap, and only for a device the
  record targets.
- `add_member`/`remove_member`: `device_group::add_device`/`remove_device`
  (group admin; adding needs the custodied Device), also listing or unlisting
  the group in the inbox. `follow_group` lists a group the device already
  belongs to and is permissionless (it records verified membership only).
  `forget_group` is operator-only.
- `index_members(group, limit)`: the group admin, resumable; moves up to
  `limit` members from `device_ids` into `MemberKey` fields and marks the
  group indexed. New members of an indexed group go to the index.

Announcements and inbox group lists are hints, never authority. A device
applies a deployment only after the same record, creator, target, operator
and membership checks as for a `DeploymentCreated` event, and the chain
checks reports as before.

## Compatibility and migration

- `contains_device` checks the index first, then `device_ids`: O(1) once an
  indexed group's vector is empty. Group reports and Seal policy use it.
- Older packages still see only `device_ids`. Their `add_device` on an indexed
  group puts the device in the vector (still a member); `remove_device` clears
  both places; `index_members` drops such duplicates. Their
  `remove_device` cannot remove an indexed member, and their group reports
  reject indexed members (fail closed).
- Older daemons and dApps do not see indexed members. Index a group only once
  its devices and tools run SF-125 clients (the dApp's group panel warns).
- `delete_group` removes the index marker; remaining `MemberKey` fields are
  orphaned with the group (remove members first to reclaim their storage).
- The manifest records all-or-none optional type origins `fleet_inbox`
  (`INBOX_TYPES`). Without them every client keeps its previous behaviour.

## Who announces

- dApp: one-shot deployments announce to target groups the sender
  administers and target devices it operates, creating a missing inbox in the
  same transaction; staged rollouts announce to members' existing inboxes;
  adding an activated device to a group lists the group in its inbox,
  creating a missing inbox before the custody loan (`new_inbox` needs the
  device in custody). The group panel's "Index members" runs one bounded batch and lists the group in
  operated members' inboxes.
- Indexer API and `suifleet-indexer ctl`: create-deployment announces to
  every target group and activated target device; add/remove-devices use
  `add_member`/`remove_member`. `ctl` rebuilds the same calls from its own
  chain reads before signing.
- Not announced: catalog deployments (`component_catalog::create_deployment`
  shares the record internally), delegated role deployments (the delegate is
  not the creator) and deployments from older clients or packages. Devices
  find these with the bounded event scan below. Desired-state revisions are
  read from the group on every poll.

## Daemon

With `fleet_inbox` origins in the manifest, every poll:

1. reads the device's inbox at its derived address and follows the groups it
   lists;
2. reads every followed group's head;
3. for each head whose `seq` changed, applies the components whose deployment
   changed since it last acted, through the ordinary checks. A record created
   before the head's baseline is not applied: the enrollment baseline (chain
   time), the first poll that read heads after an upgrade, or, for a group
   followed later, when it was first seen — matching the event baseline;
4. runs the `DeploymentCreated` and `DeviceAddedToGroup` scan only every
   `runtime.event_scan_seconds` (default 60; 0 scans every poll), so unannounced deployments arrive within a minute, as a
   safety net for unannounced deployments. Without inbox origins the scan runs
   every poll as before.

If the node has pruned the history at the device's cursor (or rejects the
range with `OUT_OF_RANGE`), a device with inbox origins logs it, moves the
cursor to the tip and continues from objects: it resynchronizes from its inbox
and group heads alone. Unannounced deployments created in the pruned range are
not applied. Without inbox origins pruned history still stops polling with an
explicit error.

The daemon sleeps until the earliest announced scheduled start or deferral
admission change (scheduled start, window bounds, end of notice, expiry)
when that comes before the next poll, so scheduled rollouts start on time
whatever the poll interval.

Indexed membership is read as the group's `MemberKey` field for this device
(one derived-ID read), so a group read is about 0.2 KB instead of the whole
member vector.

SF-122 batches the daemon's steady-state reads into one version check. The
inbox object and each followed group's `HeadKey` field are the watch-set
entries this feature adds; until SF-122 merges they are direct reads.

## Tests

- Move: `contracts/tests/fleet_inbox_tests.move` (authority, targets, custody,
  derivation, head bounds and scheduling, migration, indexed reports), plus
  three authority mutations in `scripts/check_move_security.py`.
- Simulator (`make test-ota`): `scripts/test_inbox_ota.py` — baseline, inbox
  and indexed-group deployments without events, an on-time scheduled start
  with a 60 s poll, resync after pruned history, the event safety net.
- dApp: `scripts/test_inbox.cjs` (in `make check-interfaces`).
- Indexer: `txbuild` unit test of announced call shapes.
- Real localnet across an upgrade of main's contracts:
  `make test-inbox-localnet` (start a disposable node first) — origins,
  in-place indexing of an existing group, old-package report fails closed,
  a first membership creating the inbox in the builders' order (the reverse
  aborts), stranger announcement aborts, and the real daemon through
  `examples/grpc_prune_proxy.rs`: inbox and group-head deployments, resync
  beyond pruned history and the event safety net, judged by on-chain
  receipts.

Public-testnet acceptance runs through the automatic testnet package-upgrade
gate ([TESTNET.md](TESTNET.md)); physical and mainnet acceptance stay in
SF-031/#32 and SF-087/#159.
