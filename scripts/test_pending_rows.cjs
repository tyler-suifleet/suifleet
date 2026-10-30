// Pending list rows after a confirmed transaction (SF-170): creations stay
// visible until listed, deletions until gone, then time out to a refresh hint.
//   node scripts/test_pending_rows.cjs
const assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const { outstanding, eventIds, PENDING_TIMEOUT_MS } = loader()("lib/pending-rows.ts");
const { bcs } = appRequire("@mysten/sui/bcs");

const id = (n) => "0x" + n.toString(16).padStart(64, "0");
const created = { id: "0xa", label: "canaries", kind: "create", since: 1000 };
const deleted = { id: id(0xb), label: "old", kind: "delete", since: 1000 };

// Until the list loads, nothing settles.
assert.deepEqual(outstanding([created, deleted], undefined, 1000).waiting, [created, deleted]);
// A created row waits until listed; short and full-length IDs compare equal.
assert.deepEqual(outstanding([created], [id(0xb)], 2000).waiting, [created]);
assert.deepEqual(outstanding([created], [id(0xa)], 2000).open, []);
// A deleted row waits while still listed and settles once gone.
assert.deepEqual(outstanding([deleted], ["0xb"], 2000).waiting, [deleted]);
assert.deepEqual(outstanding([deleted], [], 2000).open, []);
// After the timeout a row stops waiting (re-enabling actions) but stays visible as stale.
const late = outstanding([created], [], 1000 + PENDING_TIMEOUT_MS);
assert.deepEqual(late.waiting, []);
assert.deepEqual(late.stale, [created]);
assert.deepEqual(late.open, [created]);

// IDs come only from confirmed events of the requested type, compared normalized.
const Created = bcs.struct("GroupCreated", { group_id: bcs.Address, name: bcs.string(), admin: bcs.Address });
const event = (type, groupId) => ({ eventType: type, bcs: Created.serialize({ group_id: groupId, name: "g", admin: id(1) }).toBytes() });
const type = `${id(2)}::device_group::GroupCreated`;
assert.deepEqual(eventIds([event(type, id(7)), event(`${id(3)}::device_group::GroupCreated`, id(8)), event("0x2::device_group::GroupCreated", id(9))],
  type, (bytes) => Created.parse(bytes).group_id), [id(7), id(9)]);
assert.deepEqual(eventIds([], type, () => { throw Error("unused"); }), []);
console.log("PASS: pending rows settle on listing/removal, time out to stale, read IDs from matching confirmed events");
