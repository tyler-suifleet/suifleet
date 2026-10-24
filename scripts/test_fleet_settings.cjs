// SF-130 frontend: per-fleet cost settings defaults and bounds (shared with
// the contract and daemon through interfaces/fleet-v1.json), the owner's
// change transaction and the one-field read, against stubbed clients.
// Offline; no wallet or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loader, appRequire } = require("./frontend_test_utils.cjs");

const root = path.resolve(__dirname, "..");
const protocol = JSON.parse(fs.readFileSync(path.join(root, "interfaces/fleet-v1.json"), "utf8"));
const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifests/fixture.localnet.json"), "utf8"));
const withoutSettings = structuredClone(manifest);
for (const name of protocol.optional_type_origins.fleet_settings) manifest.type_origins[name] = manifest.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const load = loader();
const settings = load("lib/fleet-settings.ts");
const { fleet_settings_SettingsBcs } = load("lib/fleet-protocol.ts");
const { ObjectError } = appRequire("@mysten/sui/client");
const { bcs } = appRequire("@mysten/sui/bcs");
const { normalizeStructTag } = appRequire("@mysten/sui/utils");

// ── Defaults match the business plan; bounds match the contract ──
const MINUTE = 60_000, DAY = 86_400_000;
assert.deepEqual(settings.DEFAULT_SETTINGS, { checkIntervalMs: 30 * MINUTE, rolloutCheckIntervalMs: MINUTE,
  healthIntervalMs: DAY, archiveIntervalMs: 7 * DAY, archiveRetentionEpochs: 3 });
assert.deepEqual(settings.settingsErrors(settings.DEFAULT_SETTINGS), []);
assert.deepEqual(settings.SETTINGS_BOUNDS.checkIntervalMs, [5 * MINUTE, 6 * 60 * MINUTE]);
const edit = (change) => settings.settingsErrors({ ...settings.DEFAULT_SETTINGS, ...change });
for (const [name, [low, high]] of Object.entries(settings.SETTINGS_BOUNDS)) {
  const fast = name === "checkIntervalMs" ? { rolloutCheckIntervalMs: Math.min(MINUTE, low) } : {};
  assert.deepEqual(edit({ ...fast, [name]: low }), [], `${name} lower bound`);
  assert.deepEqual(edit({ [name]: high }), [], `${name} upper bound`);
  assert.equal(edit({ ...fast, [name]: low - 1 }).length, 1, `${name} below`);
  assert.equal(edit({ [name]: high + 1 }).length, 1, `${name} above`);
}
assert.deepEqual(edit({ rolloutCheckIntervalMs: 0 }), [], "fast checks off");
assert.match(edit({ checkIntervalMs: 5 * MINUTE, rolloutCheckIntervalMs: 6 * MINUTE }).join(), /must not exceed/);
assert.match(edit({ healthIntervalMs: 1.5 }).join(), /whole number/);
console.log("PASS: defaults equal the business plan; inclusive bounds and the fast-check rule match the contract");

// ── The owner's change transaction ──
const BUDGET = "0x" + "b0".repeat(32), CAP = "0x" + "c0".repeat(32);
const chosen = { ...settings.DEFAULT_SETTINGS, checkIntervalMs: 15 * MINUTE, rolloutCheckIntervalMs: 0, archiveRetentionEpochs: 13 };
const tx = settings.buildSetFleetSettingsTx(BUDGET, CAP, chosen);
const data = tx.getData();
const calls = data.commands.filter((c) => c.$kind === "MoveCall");
assert.equal(calls.length, 1);
assert.equal(`${calls[0].MoveCall.module}::${calls[0].MoveCall.function}`, "fleet_settings::set");
const args = calls[0].MoveCall.arguments.map((a) => data.inputs[a.Input]);
assert.deepEqual(args.slice(0, 2).map((i) => i.UnresolvedObject.objectId), [BUDGET, CAP]);
assert.equal(args[7].UnresolvedObject.objectId, "0x" + "0".repeat(63) + "6");
const pure = (i, type) => bcs[type]().parse(Uint8Array.from(Buffer.from(args[i].Pure.bytes, "base64")));
assert.deepEqual([pure(2, "u64"), pure(3, "u64"), pure(4, "u64"), pure(5, "u64"), pure(6, "u32")],
  [String(15 * MINUTE), "0", String(DAY), String(7 * DAY), 13]);
assert.throws(() => settings.buildSetFleetSettingsTx(BUDGET, CAP, { ...chosen, checkIntervalMs: MINUTE }), /Invalid fleet settings/);
assert.equal(settings.settingsSupported(withoutSettings), false);
console.log("PASS: the change is one fleet_settings::set call with the budget, owner cap, clock and exact u64/u32 values; invalid values are refused before signing");

// ── Reading the record ──
const valueType = `${manifest.call_package_id}::fleet_settings::Settings`;
const record = (change = {}) => fleet_settings_SettingsBcs.serialize({ version: 1, check_interval_ms: 1_800_000,
  rollout_check_interval_ms: 60_000, health_interval_ms: DAY, archive_interval_ms: 7 * DAY, archive_retention_epochs: 3, ...change }).toBytes();
const client = (bytes, type = valueType) => ({
  getDynamicField: async ({ parentId, name }) => {
    assert.equal(parentId, BUDGET);
    assert.equal(normalizeStructTag(name.type), normalizeStructTag(`${manifest.call_package_id}::fleet_settings::SettingsKey`));
    assert.deepEqual(Array.from(name.bcs), [0], "empty key struct");
    if (!bytes) throw new ObjectError("notFound", "missing field", { reason: "notFound" });
    return { dynamicField: { value: { type, bcs: bytes } } };
  },
});
(async () => {
  assert.equal(await settings.fetchFleetSettings(client(undefined), BUDGET), undefined, "no record");
  assert.deepEqual(await settings.fetchFleetSettings(client(record()), BUDGET), settings.DEFAULT_SETTINGS);
  await assert.rejects(settings.fetchFleetSettings(client(record(), "0x2::object::ID"), BUDGET), /Unexpected fleet settings type/);
  await assert.rejects(settings.fetchFleetSettings(client(record({ version: 2 })), BUDGET), /needs a newer dashboard/);
  await assert.rejects(settings.fetchFleetSettings(client(record({ check_interval_ms: 1000 })), BUDGET), /outside the contract bounds/);
  console.log("PASS: the record is read from one budget dynamic field; absent, wrong-type, newer and out-of-bounds records are handled");
})().catch((error) => { console.error(error); process.exit(1); });
