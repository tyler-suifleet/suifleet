// Arbitrary group IDs must neither break history nor invent effective targets.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire, manifestClient } = require("./frontend_test_utils.cjs");
const m = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), sui = load("lib/sui.ts"), schema = load("lib/move-bcs.ts");
const { normalizeSuiAddress: id } = appRequire("@mysten/sui/utils");
const { ObjectError } = appRequire("@mysten/sui/client");
const owner = id("0xa"), other = id("0xb"), groupId = id("0xbe"), direct = id("0xde"), member = id("0xdf");
const groupType = `${m.type_origins["device_group::DeviceGroup"]}::device_group::DeviceGroup`;
const fields = {id:groupId,name:"Lab",description:"targets",admin:owner,device_ids:[member],created_at:123,schema_version:1};
function group(overrides = {}, type = groupType) { return {objectId:groupId,type,content:schema.GroupBcs.serialize({...fields,...overrides}).toBytes()}; }
function client(target, creators = [owner, owner], shared = false) {
  const c = manifestClient(m);
  const events = [id("0xf1"), id("0xf2")].map((deployment, index) => ({
    deployment_id:deployment,component_name:"app",version:"1",recipe_blob_id:"recipe",artifact_sha256:"a".repeat(64),
    target_groups:index === 0 || shared ? [groupId] : [],target_devices:[direct],creator:creators[index],
  }));
  const records = new Map(events.map(e => [e.deployment_id, {objectId:e.deployment_id,content:schema.DeploymentBcs.serialize({
    id:e.deployment_id,creator:e.creator,component_name:e.component_name,version:e.version,recipe_blob_id:e.recipe_blob_id,
    artifact_sha256:e.artifact_sha256,created_at:456,schema_version:1,
  }).toBytes()}]));
  c.listEvents = async () => ({events:events.map(e => ({bcs:schema.DeploymentCreatedBcs.serialize(e).toBytes()})),hasNextPage:false});
  c.getObjects = async ({objectIds}) => ({objects:objectIds.map(objectId => objectId === groupId ? target : records.get(objectId))});
  c.getDynamicField = async () => { throw new ObjectError("missing", "not found", {reason:"notFound"}); };
  return c;
}
(async () => {
  const valid = await sui.fetchDeploymentEvents(client(group()), owner);
  assert.equal(valid.length, 2);
  assert.deepEqual(valid[0].resolvedDeviceIds, [direct,member]);
  assert.deepEqual(valid[1].resolvedDeviceIds, [direct]);
  for (const [label, target] of [
    ["non-group decodable bytes",group({}, `${m.type_origins["device_registry::Device"]}::device_registry::Device`)],
    ["actual Device bytes",{objectId:groupId,type:`${m.type_origins["device_registry::Device"]}::device_registry::Device`,content:Uint8Array.of(1)}],
    ["unsupported schema",group({schema_version:2})], ["foreign admin",group({admin:other})],
    ["malformed BCS",{objectId:groupId,type:groupType,content:Uint8Array.of(1)}],
    ["invalid type tag",{...group(),type:"not a type"}],
    ["deleted",new ObjectError("missing", "deleted", {reason:"deleted"})],
  ]) {
    const history = await sui.fetchDeploymentEvents(client(target), owner);
    assert.equal(history.length, 2, label);
    assert.deepEqual(history[0].resolvedDeviceIds, [direct], label);
    assert.deepEqual(history[1].resolvedDeviceIds, [direct], label);
  }
  await assert.rejects(sui.fetchDeploymentEvents(client(new ObjectError("bad", "transport unavailable", {reason:"unknown"})), owner), /transport unavailable/);
  const shared = client(group({admin:other}), [owner,other], true), read = shared.getObjects, reads = [];
  shared.getObjects = async (args) => {reads.push(...args.objectIds);return read(args);};
  const history = await sui.fetchDeploymentEvents(shared);
  assert.equal(reads.filter(id => id === groupId).length, 1, "shared group read once for all deployments");
  assert.deepEqual(history[0].resolvedDeviceIds, [direct], "foreign administrator ignored per deployment");
  assert.deepEqual(history[1].resolvedDeviceIds, [direct,member], "matching administrator resolved per deployment");
  console.log("PASS: valid groups resolve; wrong types/layouts/versions/admins/deleted targets preserve history; transport errors remain visible");
})().catch(error => {console.error(error);process.exitCode=1;});
