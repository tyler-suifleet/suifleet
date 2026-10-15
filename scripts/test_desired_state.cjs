// Desired-state (SF-013) frontend helpers: revision transactions, reads and conflicts.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const m = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const desired = loader()("lib/desired-state.ts");
const protocol = loader()("lib/fleet-protocol.ts");
const { normalizeSuiAddress: id } = appRequire("@mysten/sui/utils");
const { ObjectError } = appRequire("@mysten/sui/client");
const g1 = id("0x61"), g2 = id("0x62"), d1 = id("0xd1"), d2 = id("0xd2"), r1 = id("0xa1"), r2 = id("0xa2");
const change = { componentName: "firmware", version: "1.1.0", recipeBlobId: "recipe", artifactSha256: "a".repeat(64), recipeSha256: "b".repeat(64) };

function targets(tx) {
  return tx.getData().commands.map((command) => command.MoveCall && `${command.MoveCall.module}::${command.MoveCall.function}`);
}

function field(state, type = `${m.type_origins["desired_state::DesiredState"]}::desired_state::DesiredState`) {
  return { dynamicField: { value: { type, bcs: protocol.desired_state_DesiredStateBcs.serialize(state).toBytes() } } };
}

(async () => {
  // One record for all groups; each group gets its own begin/set/commit; shared last.
  assert.deepEqual(targets(desired.buildDesiredRevisionTx([g1, g2], change)), [
    "deployment_manager::new_deployment_verified",
    "desired_state::begin", "desired_state::set_component", "desired_state::commit",
    "desired_state::begin", "desired_state::set_component", "desired_state::commit",
    "deployment_manager::share_deployment"]);
  assert.deepEqual(targets(desired.buildDesiredRevisionTx([g1], undefined, ["agent", "logger"])), [
    "desired_state::begin", "desired_state::remove_component", "desired_state::remove_component", "desired_state::commit"]);
  assert.throws(() => desired.buildDesiredRevisionTx([], change), /at least one group/);
  assert.throws(() => desired.buildDesiredRevisionTx([g1]), /must change something/);
  // The new record targets the groups only (never devices directly).
  const created = desired.buildDesiredRevisionTx([g1, g2], change).getData();
  const create = created.commands[0].MoveCall;
  const vectors = create.arguments.slice(5, 7).map((arg) => created.inputs[arg.Input].Pure.bytes);
  const decode = (b64) => appRequire("@mysten/sui/bcs").bcs.vector(appRequire("@mysten/sui/bcs").bcs.Address).parse(Buffer.from(b64, "base64"));
  assert.deepEqual(decode(vectors[0]), [g1, g2]);
  assert.deepEqual(decode(vectors[1]), []);

  // Reads: absent = no desired state; wrong type and transport errors surface.
  const state = { revision: 3, updated_at: 99, components: { contents: [{ key: "firmware", value: r1 }] } };
  const keys = [];
  const read = await desired.readDesiredState({ getDynamicField: async (args) => { keys.push(args.name.type); return field(state); } }, g1);
  assert.deepEqual(read, { revision: 3, updatedAt: 99, components: { firmware: r1 } });
  assert.equal(keys[0], `${m.type_origins["desired_state::DesiredKey"]}::desired_state::DesiredKey`);
  const missing = { getDynamicField: async () => { throw new ObjectError("missing", "not found", { reason: "notFound" }); } };
  assert.equal(await desired.readDesiredState(missing, g1), undefined);
  await assert.rejects(desired.readDesiredState({ getDynamicField: async () => field(state, "0x2::coin::Coin") }, g1), /Unexpected DesiredState type/);
  await assert.rejects(desired.readDesiredState({ getDynamicField: async () => { throw new ObjectError("bad", "transport unavailable", { reason: "unknown" }); } }, g1), /transport unavailable/);
  const states = await desired.fetchDesiredStates({ getDynamicField: async (args) => {
    if (args.parentId === g2) throw new ObjectError("missing", "not found", { reason: "notFound" });
    return field(state);
  } }, [g1, g2]);
  assert.deepEqual(Object.keys(states), [g1]);

  // Conflicts: only devices in both groups, only components named differently.
  const groups = [{ objectId: g1, deviceIds: [d1, d2] }, { objectId: g2, deviceIds: [d2] }];
  const both = { [g1]: { revision: 1, updatedAt: 0, components: { firmware: r1, agent: r2 } },
                 [g2]: { revision: 4, updatedAt: 0, components: { firmware: r2, agent: r2 } } };
  assert.deepEqual(desired.desiredConflicts(groups, both), [{ deviceId: d2, component: "firmware", choices: { [g1]: r1, [g2]: r2 } }]);
  assert.deepEqual(desired.desiredConflicts(groups, { [g1]: both[g1] }), []);
  console.log("PASS: desired-state revisions (one record, per-group atomic commits, group-only targets), reads and cross-group conflicts");
})().catch((error) => { console.error(error); process.exitCode = 1; });
