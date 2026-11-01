// SF-174: recurring weekly windows in the dApp. The instants below are the
// daemon's own vectors (suifleet-edge/src/control.rs, computed independently
// with Python zoneinfo), so the editor preview and the device agree.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const base = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
const { ObjectError } = appRequire("@mysten/sui/client");
const H = 3_600_000, M = 60_000;

// A pre-upgrade manifest: no recurring types, so nothing is read or built.
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(base);
{
  const c = loader()("lib/deployment-control.ts");
  assert.equal(c.recurringSupported(), false);
  assert.throws(() => c.buildRecurringWindowTx("0x42", { timezone: "UTC", slots: [{ days: 1, start_minute: 0, duration_minutes: 60 }] }), /upgraded package/);
  (async () => assert.equal(await c.readRecurringWindow({ getDynamicField: async () => { throw Error("must not read"); } }, "0x42"), null))();
}

const m = { ...base, type_origins: { ...base.type_origins } };
for (const name of ["RecurringWindowKey", "RecurringWindow", "WeeklySlot"]) m.type_origins[`deployment_manager::${name}`] = "0x5";
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader();
const c = load("lib/deployment-control.ts"), r = load("lib/recurring-window.ts"), protocol = load("lib/fleet-protocol.ts");
const weekly = (timezone, slots) => ({ revision: "1", timezone, slots: slots.map(([days, start_minute, duration_minutes]) => ({ days, start_minute, duration_minutes })) });
const open = { not_before_ms: "0", expires_ms: "0", window_start_ms: "0", window_end_ms: "0", state: 0 };

(async () => {
  // Same vectors as the daemon: site-zone Sunday, DST gap/overlap, midnight.
  const berlin = weekly("Europe/Berlin", [[64, 120, 120]]);
  const sunday = 1_793_494_800_000, saturdayNoon = 1_793_444_400_000;
  assert.deepEqual(r.nextAdmissionWindows(berlin, open, saturdayNoon),
    [0, 1, 2].map(week => ({ start: sunday + week * 168 * H, end: sunday + week * 168 * H + 2 * H })));
  assert.deepEqual(r.occurrences(weekly("Europe/Berlin", [[64, 150, 60]]), 1_774_746_000_000 - H, 1)[0], { start: 1_774_746_000_000, end: 1_774_746_000_000 + H });
  assert.deepEqual(r.occurrences(weekly("America/New_York", [[64, 135, 30]]), 1_772_953_200_000 - H, 1)[0], { start: 1_772_953_200_000, end: 1_772_953_200_000 + 30 * M });
  assert.deepEqual(r.occurrences(weekly("Europe/Berlin", [[64, 150, 60]]), 1_792_888_200_000 - 1, 1)[0], { start: 1_792_888_200_000, end: 1_792_891_800_000 });
  assert.equal(r.occurrences(weekly("Europe/Berlin", [[64, 150, 60]]), 1_792_891_800_000 + 15 * M, 8)[0].start, 1_793_496_600_000);
  assert.deepEqual(r.occurrences(weekly("America/New_York", [[64, 90, 30]]), 1_793_511_000_000, 1)[0], { start: 1_793_511_000_000, end: 1_793_511_000_000 + 30 * M });
  assert.deepEqual(r.occurrences(weekly("Asia/Kolkata", [[64, 1380, 180]]), 1_793_561_400_000, 1)[0], { start: 1_793_554_200_000, end: 1_793_565_000_000 });
  assert.deepEqual(r.occurrences(weekly("Asia/Kolkata", [[64, 1380, 60], [1, 0, 60]]), 1_793_554_200_000 + 90 * M, 1)[0], { start: 1_793_554_200_000, end: 1_793_554_200_000 + 2 * H });
  assert.equal(r.recurringActive(berlin, sunday), true);
  assert.equal(r.recurringActive(berlin, sunday + 2 * H), false);

  // Combined with not-before, expiry and the absolute window.
  assert.deepEqual(r.nextAdmissionWindows(berlin, { ...open, not_before_ms: String(sunday + H) }, saturdayNoon)[0], { start: sunday + H, end: sunday + 2 * H });
  assert.deepEqual(r.nextAdmissionWindows(berlin, { ...open, expires_ms: String(sunday + 168 * H + 30 * M) }, saturdayNoon),
    [{ start: sunday, end: sunday + 2 * H }, { start: sunday + 168 * H, end: sunday + 168 * H + 30 * M }]);
  assert.deepEqual(r.nextAdmissionWindows(berlin, { ...open, window_start_ms: String(sunday + 3 * H), window_end_ms: String(sunday + 168 * H + H) }, saturdayNoon),
    [{ start: sunday + 168 * H, end: sunday + 168 * H + H }]);
  assert.deepEqual(r.nextAdmissionWindows(berlin, { ...open, state: 2 }, saturdayNoon), []);
  assert.deepEqual(r.nextAdmissionWindows(null, { ...open, not_before_ms: "10" }, 5), [{ start: 10, end: Infinity }]);

  // Status labels.
  const control = { ...c.immediateControl, revision: "2" };
  assert.match(c.controlReason(control, saturdayNoon, berlin), /Outside recurring/);
  assert.match(c.controlReason(control, sunday, berlin), /eligible/);
  assert.match(c.controlReason({ ...control, state: 1 }, sunday, berlin), /Paused/);
  assert.match(c.controlReason(control, sunday, weekly("Mars/Olympus_Mons", [[64, 120, 120]])), /unsupported timezone/);

  // Transactions: direct creator and customer fleet, plus clear.
  const set = c.buildRecurringWindowTx("0x42", berlin).getData().commands;
  assert.equal(set.length, 1);
  assert.equal(set[0].MoveCall.module, "deployment_manager");
  assert.equal(set[0].MoveCall.function, "set_recurring_window");
  assert.equal(set[0].MoveCall.arguments.length, 5);
  const fleet = { id: "0x71", customerId: "0x70", name: "Production", administrator: true };
  const fleetSet = c.buildRecurringWindowTx("0x42", berlin, fleet).getData().commands[0].MoveCall;
  assert.deepEqual([fleetSet.module, fleetSet.function, fleetSet.arguments.length], ["customer", "set_recurring_window", 7]);
  assert.equal(c.buildRecurringWindowTx("0x42", null).getData().commands[0].MoveCall.function, "clear_recurring_window");
  assert.equal(c.buildRecurringWindowTx("0x42", null, fleet).getData().commands[0].MoveCall.module, "customer");
  for (const bad of [
    weekly("Mars/Olympus_Mons", [[64, 120, 120]]), weekly("device-local", [[64, 120, 120]]), weekly("/etc/localtime", [[64, 120, 120]]),
    weekly("UTC", []), weekly("UTC", [[0, 0, 1]]), weekly("UTC", [[128, 0, 1]]), weekly("UTC", [[1, 1440, 1]]),
    weekly("UTC", [[1, 0, 0]]), weekly("UTC", [[1, 0, 1441]]), weekly("UTC", Array(9).fill([1, 0, 1])),
  ]) assert.throws(() => c.buildRecurringWindowTx("0x42", bad), undefined, JSON.stringify(bad));

  // Zone spelling: the daemon resolves names case-sensitively against its
  // bundled tzdb (control.rs rejects "europe/berlin"), so the editor saves its
  // exact spelling, keeps tzdb aliases as typed and never previews a miscased
  // stored zone as admitting.
  const storedZone = tx => tx.getData().inputs.map(i => i.Pure && Buffer.from(i.Pure.bytes, "base64").toString("latin1")).find(b => b && /\//.test(b));
  assert.equal(r.canonicalZone("europe/berlin"), "Europe/Berlin");
  assert.equal(r.canonicalZone("Asia/Calcutta"), "Asia/Calcutta");
  assert.equal(r.canonicalZone("asia/calcutta"), "Asia/Calcutta");
  assert.equal(r.canonicalZone("Mars/Olympus_Mons"), undefined);
  assert.throws(() => r.validateRecurring(weekly("europe/berlin", [[64, 120, 120]])), /spelled exactly Europe\/Berlin/);
  assert.throws(() => c.buildRecurringWindowTx("0x42", weekly("europe/berlin", [[64, 120, 120]])), /spelled exactly/);
  assert.match(storedZone(c.buildRecurringWindowTx("0x42", berlin)), /^\x0dEurope\/Berlin$/);
  assert.match(storedZone(c.buildRecurringWindowTx("0x42", weekly("Asia/Calcutta", [[64, 120, 120]]))), /^\x0dAsia\/Calcutta$/);
  assert.match(c.controlReason(control, sunday, weekly("europe/berlin", [[64, 120, 120]])), /unsupported timezone \(europe\/berlin\); devices defer/);
  const tzdb = load("lib/tzdb-names.ts");
  assert.ok(tzdb.TZDB_NAMES.includes("Europe/Berlin") && !tzdb.TZDB_NAMES.includes("europe/berlin") && /^20\d\d[a-z]$/.test(tzdb.TZDB_VERSION));
  assert.ok(r.knownZones().every(z => tzdb.TZDB_NAMES.includes(z)) && r.knownZones().includes("Europe/Berlin"));

  // Reads: introduction-origin key/value types, absent field, transport error.
  let key;
  const value = { type: `0x5::deployment_manager::RecurringWindow`, bcs: protocol.deployment_manager_RecurringWindowBcs.serialize(berlin).toBytes() };
  assert.deepEqual(await c.readRecurringWindow({ getDynamicField: async args => { key = args.name.type; return { dynamicField: { value } }; } }, "0x42"), berlin);
  assert.equal(key, `${"0x" + "5".padStart(64, "0")}::deployment_manager::RecurringWindowKey`);
  assert.equal(await c.readRecurringWindow({ getDynamicField: async () => { throw new ObjectError("missing", "missing", { reason: "notFound" }); } }, "0x42"), null);
  await assert.rejects(c.readRecurringWindow({ getDynamicField: async () => { throw Error("transport down"); } }, "0x42"), /transport down/);
  await assert.rejects(c.readRecurringWindow({ getDynamicField: async () => ({ dynamicField: { value: { ...value, type: "0x5::deployment_manager::Control" } } }) }, "0x42"), /Unexpected recurring/);

  // The editor shows the next three admission windows in local and UTC time.
  const React = appRequire("react"), { renderToStaticMarkup } = appRequire("react-dom/server");
  const { ControlEditor } = load("components/DeploymentControlEditor.tsx");
  const daily = weekly("Asia/Tokyo", [[127, 120, 60]]);
  const html = renderToStaticMarkup(React.createElement(ControlEditor, { control, recurring: daily, recurringSupported: true,
    canEdit: true, pending: false, onSave() {}, onRecurring() {} }));
  assert.match(html, /Mon, Tue, Wed, Thu, Fri, Sat, Sun 02:00 for 1 h \(Asia\/Tokyo\)/);
  assert.equal((html.match(/<li>/g) || []).length, 3);
  assert.match(html, /UTC \d{4}-\d\d-\d\dT17:00:00\.000Z/); // 02:00 JST
  assert.match(html, /Remove recurring window/);
  const miscased = renderToStaticMarkup(React.createElement(ControlEditor, { control, recurring: weekly("europe/berlin", [[127, 120, 60]]),
    recurringSupported: true, canEdit: true, pending: false, onSave() {}, onRecurring() {} }));
  assert.equal((miscased.match(/<li>/g) || []).length, 0);
  assert.match(miscased, /None: devices defer \(Timezone must be spelled exactly Europe\/Berlin\)/);
  const legacy = renderToStaticMarkup(React.createElement(ControlEditor, { control, recurringSupported: false, canEdit: true, pending: false, onSave() {} }));
  assert.match(legacy, /need the upgraded package/);
  console.log("PASS: recurring windows match daemon DST/midnight vectors, combine with controls, build/read origin-bound calls and preview three admissions");
})().catch(e => { console.error(e); process.exitCode = 1; });
