const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const m = JSON.parse(fs.readFileSync(path.resolve(__dirname,"../manifests/fixture.localnet.json")));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), c = load("lib/deployment-control.ts"), protocol = load("lib/fleet-protocol.ts");
const { ObjectError } = appRequire("@mysten/sui/client");
(async () => {
  const valid = { ...c.immediateControl, revision: "4", not_before_ms: "10", expires_ms: "100", window_start_ms: "20", window_end_ms: "80", notice_ms: "5" };
  const sui = load("lib/sui.ts"), desired = load("lib/desired-state.ts");
  const paused = sui.buildCreateDeploymentTx("app", "1", "recipe", "a".repeat(64), "b".repeat(64), ["0x61"], [], true).getData();
  assert.deepEqual(paused.commands.map(cmd => cmd.MoveCall.function), ["new_deployment_verified", "set_control", "share_deployment"]);
  const revision = desired.buildDesiredRevisionTx(["0x61"], {componentName:"app",version:"1",recipeBlobId:"recipe",artifactSha256:"a".repeat(64),recipeSha256:"b".repeat(64),initiallyPaused:true}).getData();
  assert.deepEqual(revision.commands.slice(-2).map(cmd => cmd.MoveCall.function), ["set_control", "share_deployment"]);
  const tx = c.buildDeploymentControlTx("0x42", valid).getData();
  assert.equal(tx.commands[0].MoveCall.function, "set_control");
  assert.equal(tx.commands[0].MoveCall.arguments.length, 7);
  assert.match(c.controlReason(valid, 9), /Scheduled/);
  assert.match(c.controlReason(valid, 19), /Outside maintenance/);
  assert.match(c.controlReason(valid, 20), /advance notice/);
  assert.match(c.controlReason(valid, 80), /Outside maintenance/);
  assert.match(c.controlReason(valid, 100), /Expired/);
  assert.match(c.controlReason({ ...valid, state: 1 }, 30), /Paused/);
  assert.match(c.controlReason({ ...valid, state: 2 }, 30), /Cancelled permanently/);
  for (const bad of [{state:3}, {expires_ms:"10"}, {window_end_ms:"20"}, {notice_ms:"604800001"}, {expires_ms:"-1"}, {expires_ms:"18446744073709551616"}]) {
    assert.throws(() => c.buildDeploymentControlTx("0x42", {...valid,...bad}));
  }
  let key;
  const field = { dynamicField: { value: { type: `${m.type_origins["deployment_manager::Control"]}::deployment_manager::Control`, bcs: protocol.deployment_manager_ControlBcs.serialize(valid).toBytes() } } };
  assert.deepEqual(await c.readDeploymentControl({getDynamicField: async args => {key=args.name.type; return field;}}, "0x42"), valid);
  assert.equal(key, `${m.type_origins["deployment_manager::ControlKey"]}::deployment_manager::ControlKey`);
  assert.deepEqual(await c.readDeploymentControl({getDynamicField: async () => {throw new ObjectError("missing", "missing", {reason:"notFound"});}}, "0x42"), c.immediateControl);
  await assert.rejects(c.readDeploymentControl({getDynamicField: async () => {throw Error("transport down");}}, "0x42"), /transport down/);
  await assert.rejects(c.readDeploymentControl({getDynamicField: async () => ({dynamicField:{value:{...field.dynamicField.value,type:"0x2::coin::Coin"}}})}, "0x42"), /Unexpected deployment/);
  console.log("PASS: deployment controls, deadline/window labels, strict validation, mixed-origin BCS and fail-closed reads");
})().catch(e=>{console.error(e);process.exitCode=1;});
