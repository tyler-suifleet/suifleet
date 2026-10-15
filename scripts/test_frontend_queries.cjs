// Exercise actual query/BCS code across pagination, deleted records and failures.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire, manifestClient } = require("./frontend_test_utils.cjs");
const m = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), sui = load("lib/sui.ts"), schema = load("lib/move-bcs.ts");
const { normalizeSuiAddress: id } = appRequire("@mysten/sui/utils"), { ObjectError } = appRequire("@mysten/sui/client");
const owner = id("0xa"), device = id("0xde"), group = id("0xbe"), deployment = id("0xfa"), custodied = id("0xdc"), ownerCap = id("0xc1");
const deviceBytes = schema.DeviceBcs.serialize({ id: device, name: "Pi", device_address: id("0xda"), arch: "aarch64", meta: { contents: [] }, registered_at: 123, schema_version: 1 }).toBytes();
const custodiedBytes = schema.DeviceBcs.serialize({ id: custodied, name: "Pi 2", device_address: id("0xdb"), arch: "aarch64", meta: { contents: [] }, registered_at: 124, schema_version: 1 }).toBytes();
const groupBytes = schema.GroupBcs.serialize({ id: group, name: "Lab", description: "Test fleet", admin: owner, device_ids: [device], created_at: 123, schema_version: 1 }).toBytes();
const event = { deployment_id: deployment, component_name: "app", version: "1", recipe_blob_id: "recipe", artifact_sha256: "a".repeat(64), target_groups: [group], target_devices: [device], creator: owner };
const deploymentBytes = schema.DeploymentBcs.serialize({ id: deployment, creator: owner, component_name: "app", version: "1", recipe_blob_id: "recipe", artifact_sha256: "a".repeat(64), created_at: 456, schema_version: 1 }).toBytes();
(async () => {
  const c = manifestClient(m), ownedCursors = [], eventCursors = [];
  c.listOwnedObjects = async (args) => {
    // Activated devices are held by the registry and listed through their owner caps.
    if (args.type.endsWith("::device_registry::DeviceOwnerCap")) {
      return { objects: [{ objectId: ownerCap, content: schema.DeviceOwnerCapBcs.serialize({ id: ownerCap, device_id: custodied }).toBytes() }], hasNextPage: false };
    }
    assert.ok(args.type.endsWith("::device_registry::Device"));
    ownedCursors.push(args.cursor);
    return args.cursor ? { objects: [{ objectId: device, content: deviceBytes }], hasNextPage: false } : { objects: [], hasNextPage: true, cursor: "owned-next" };
  };
  c.getObjects = async ({ objectIds }) => { assert.deepEqual(objectIds, [custodied]); return { objects: [{ objectId: custodied, content: custodiedBytes }] }; };
  const devices = await sui.fetchDevices(c, owner);
  assert.deepEqual(devices.map((d) => [d.objectId, d.ownerCapId, d.registeredAt]), [[custodied, ownerCap, 124], [device, undefined, 123]]);
  assert.deepEqual(ownedCursors, [undefined, "owned-next"]);
  c.listEvents = async (args) => {
    eventCursors.push(args.before);
    if (!args.before) return { events: [], hasNextPage: true, endCursor: "scanned-next" };
    const bytes = args.filter.eventType.endsWith("GroupCreated") ? schema.GroupCreatedBcs.serialize({ group_id: group, name: "Lab", admin: owner }).toBytes() : schema.DeploymentCreatedBcs.serialize(event).toBytes();
    return { events: [{ bcs: bytes, json: null }], hasNextPage: false };
  };
  c.getObjects = async ({ objectIds }) => ({ objects: objectIds.map((objectId) => ({ objectId, type: `${m.type_origins["device_group::DeviceGroup"]}::device_group::DeviceGroup`, content: objectId === group ? groupBytes : deploymentBytes })) });
  assert.deepEqual((await sui.fetchGroups(c, owner))[0].deviceIds, [device]);
  assert.deepEqual(eventCursors, [undefined, "scanned-next"]);
  c.getDynamicField = async () => ({ dynamicField: { value: { type: `${m.type_origins["deployment_manager::Receipts"]}::deployment_manager::Receipts`, bcs: schema.ReceiptsBcs.serialize({ version: 2, statuses: { contents: [{ key: device, value: 1 }] } }).toBytes() } } });
  const deployments = await sui.fetchDeploymentEvents(c, owner);
  assert.equal(deployments[0].timestampMs, 456); assert.equal(deployments[0].receiptsVerified, true);
  assert.deepEqual(deployments[0].resolvedDeviceIds, [device]);
  assert.deepEqual(deployments[0].statuses, { [device]: 1 });
  // Empty recipe bytes are valid in legacy records; only the authenticated schema
  // selects catalog cleanup. Exercise both queries and actual generated Move calls.
  for (const schemaVersion of ["1", "2"]) {
    const emptyBlob = schema.DeploymentBcs.serialize({ id: deployment, creator: owner, component_name: "app", version: "1",
      recipe_blob_id: "", artifact_sha256: "", created_at: 456, schema_version: schemaVersion }).toBytes();
    c.getObjects = async ({ objectIds }) => ({ objects: objectIds.map(objectId => ({ objectId,
      type: `${m.type_origins["device_group::DeviceGroup"]}::device_group::DeviceGroup`, content: objectId === group ? groupBytes : emptyBlob })) });
    const [record] = await sui.fetchDeploymentEvents(c, owner);
    assert.equal(record.schemaVersion, schemaVersion);
    assert.equal(record.recipeBlobId, "");
    const call = sui.buildDeleteDeploymentTx(record.deploymentId, record.schemaVersion).getData().commands[0].MoveCall;
    assert.equal(call.module, schemaVersion === "2" ? "component_catalog" : "deployment_manager");
    assert.equal(call.function, "delete_deployment");
  }
  assert.throws(() => sui.buildDeleteDeploymentTx(deployment, "3"), /Unsupported DeploymentRecord schema/);
  c.getObjects = async () => ({ objects: [new ObjectError("notExists", "deleted", { reason: "deleted" })] });
  assert.deepEqual(await sui.fetchDeploymentEvents(c, owner), []);
  c.getObjects = async () => ({ objects: [new ObjectError("unknown", "transport failure", { reason: "unknown" })] });
  await assert.rejects(sui.fetchGroups(c, owner), /transport failure/);
  c.listEvents = async () => ({ events: [], hasNextPage: true, endCursor: "stuck" });
  await assert.rejects(sui.fetchGroups(c, owner), /no progress/);
  c.listOwnedObjects = async () => ({ objects: [], hasNextPage: true, cursor: null });
  await assert.rejects(sui.fetchDevices(c, owner), /no cursor/);
  console.log("PASS: BCS inventory (activated and unactivated)/group/deployment queries, empty scan pages, cursors, authoritative receipts, deleted records and errors");
})().catch((error) => { console.error(error); process.exitCode = 1; });
