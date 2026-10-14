const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { loader, appRequire, manifestClient } = require("./frontend_test_utils.cjs");
const fixture = JSON.parse(fs.readFileSync(process.argv[2] ?? path.resolve(__dirname, "../manifests/fixture.localnet.json"), "utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const { parseManifest, verifyDeployment, moveType, REQUIRED_TYPES } = loader()("lib/deployment-manifest.ts");
const copy = () => structuredClone(fixture);
(async () => {
  assert.deepEqual(parseManifest(JSON.stringify(fixture)), fixture);
  if (process.argv[2]) {
    const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
    await verifyDeployment(new SuiGrpcClient({ network: fixture.network, baseUrl: fixture.rpc_url }), fixture);
    console.log("PASS: frontend gRPC SDK validates live package, UpgradeCap and registry");
    return;
  }
  assert.equal(moveType("deployment_manager::RecipeDigestKey"), `${fixture.type_origins["deployment_manager::RecipeDigestKey"]}::deployment_manager::RecipeDigestKey`);
  assert.equal(moveType("device_cap::DeviceAddressKey"), `${fixture.type_origins["device_cap::DeviceAddressKey"]}::device_cap::DeviceAddressKey`);
  await verifyDeployment(manifestClient(fixture), fixture);
  for (const [key, value] of [["schema_version", 2], ["protocol_version", 2], ["call_package_id", "bad"], ["network", "unknown"]]) {
    const invalid = copy(); invalid[key] = value; assert.throws(() => parseManifest(JSON.stringify(invalid)));
  }
  const missing = copy(); delete missing.type_origins[REQUIRED_TYPES[0]]; assert.throws(() => parseManifest(JSON.stringify(missing)));
  const incompatible = copy(); incompatible.supported_clients.dapp.max_exclusive = "0.0.1"; assert.throws(() => parseManifest(JSON.stringify(incompatible)));
  const staleChain = copy(); staleChain.chain_identifier = "deadbeef";
  await assert.rejects(verifyDeployment(manifestClient(staleChain), fixture), /chain identifier/);
  const staleOrigin = copy(); staleOrigin.type_origins["deployment_manager::RecipeDigestKey"] = staleOrigin.call_package_id;
  await assert.rejects(verifyDeployment(manifestClient(staleOrigin), fixture), /type origin/);
  const staleCap = manifestClient(fixture), oldCap = staleCap.getObject;
  const { UpgradeCapBcs } = loader()("lib/move-bcs.ts");
  staleCap.getObject = async (args) => args.objectId === fixture.upgrade_cap_id ? { object: { type: "0x2::package::UpgradeCap", content: UpgradeCapBcs.serialize({ id: fixture.upgrade_cap_id, package: "0x4", version: 4, policy: 0 }).toBytes() } } : oldCap(args);
  await assert.rejects(verifyDeployment(staleCap, fixture), /UpgradeCap/);
  const staleVersion = copy(); staleVersion.package_version = "2";
  await assert.rejects(verifyDeployment(manifestClient(staleVersion), fixture), /package metadata/);
  const staleRegistry = manifestClient(fixture), oldRegistry = staleRegistry.getObject;
  staleRegistry.getObject = async (args) => args.objectId === fixture.shared_objects.registry.id ? { object: { type: `${fixture.type_origins["device_registry::DeviceRegistry"]}::device_registry::DeviceRegistry`, owner: { $kind: "Immutable" } } } : oldRegistry(args);
  await assert.rejects(verifyDeployment(staleRegistry, fixture), /registry/);
  // Chain proof must not reuse SDK's cached genesis digest after a node reset.
  const resetting = manifestClient(fixture);
  await verifyDeployment(resetting);
  resetting.ledgerService = manifestClient(staleChain).ledgerService;
  await assert.rejects(verifyDeployment(resetting), /chain identifier/);
  console.log("PASS: gRPC manifest BCS proof, mixed origins, stale chain/package/types/registry, incompatible clients and reset");
})().catch((error) => { console.error(error); process.exitCode = 1; });
