const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { loader, appRequire, manifestClient } = require("./frontend_test_utils.cjs");
const fixture = JSON.parse(fs.readFileSync(process.argv[2] ?? path.resolve(__dirname, "../manifests/fixture.localnet.json"), "utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const { parseManifest, verifyDeployment, moveType, REQUIRED_TYPES, CATALOG_TYPES, SEAL_POLICY_TYPES, BUDGET_TYPES, SETTINGS_TYPES } = loader()("lib/deployment-manifest.ts");
const copy = () => structuredClone(fixture);
(async () => {
  assert.deepEqual(parseManifest(JSON.stringify(fixture)), fixture);
  const ordered = copy(); ordered.walrus.publisher_urls = ['https://one.example', 'https://two.example']; ordered.walrus.aggregator_urls = ['https://read.example'];
  assert.deepEqual(parseManifest(JSON.stringify(ordered)), ordered);
  for (const value of [[], 'https://one.example', ['file:///tmp/blob'], ['https://user:secret@example.com']]) {
    const invalid = copy(); invalid.walrus.publisher_urls = value;
    assert.throws(() => parseManifest(JSON.stringify(invalid)));
  }
  if (process.argv[2]) {
    const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
    await verifyDeployment(new SuiGrpcClient({ network: fixture.network, baseUrl: fixture.rpc_url }), fixture);
    console.log("PASS: frontend gRPC SDK validates live package, UpgradeCap and registry");
    return;
  }
  assert.equal(moveType("deployment_manager::RecipeDigestKey"), `${fixture.type_origins["deployment_manager::RecipeDigestKey"]}::deployment_manager::RecipeDigestKey`);
  assert.equal(moveType("device_cap::DeviceAddressKey"), `${fixture.type_origins["device_cap::DeviceAddressKey"]}::device_cap::DeviceAddressKey`);
  await verifyDeployment(manifestClient(fixture), fixture);
  const parallel = manifestClient(fixture), originalRead = parallel.getObject, started = [], release = [];
  parallel.getObject = (args) => new Promise(resolve => {started.push(args.objectId);release.push(() => resolve(originalRead(args)));});
  const proof = verifyDeployment(parallel, fixture);
  await new Promise(setImmediate);
  try { assert.deepEqual(new Set(started), new Set([fixture.call_package_id, fixture.upgrade_cap_id, fixture.shared_objects.registry.id])); }
  finally { release.forEach(resolve => resolve()); }
  await proof;
  for (const [key, value] of [["schema_version", 2], ["protocol_version", 2], ["call_package_id", "bad"], ["network", "unknown"]]) {
    const invalid = copy(); invalid[key] = value; assert.throws(() => parseManifest(JSON.stringify(invalid)));
  }
  const catalog = copy(); catalog.type_origins[CATALOG_TYPES[0]] = "0x4";
  assert.throws(() => parseManifest(JSON.stringify(catalog)), /partial catalog/);
  for (const name of CATALOG_TYPES) catalog.type_origins[name] = "0x4";
  parseManifest(JSON.stringify(catalog));
  await verifyDeployment(manifestClient(catalog), catalog);
  const forgedCatalog = structuredClone(catalog); forgedCatalog.type_origins[CATALOG_TYPES[0]] = "0x3";
  await assert.rejects(verifyDeployment(manifestClient(forgedCatalog), catalog), /type origin/);
  const seal = copy(); seal.type_origins[SEAL_POLICY_TYPES[0]] = "0x4";
  assert.throws(() => parseManifest(JSON.stringify(seal)), /partial seal policy/);
  for (const name of SEAL_POLICY_TYPES) seal.type_origins[name] = "0x4";
  parseManifest(JSON.stringify(seal));
  await verifyDeployment(manifestClient(seal), seal);
  const forgedSeal = structuredClone(seal); forgedSeal.type_origins[SEAL_POLICY_TYPES[0]] = "0x3";
  await assert.rejects(verifyDeployment(manifestClient(forgedSeal), seal), /type origin/);
  const budget = copy(); budget.type_origins[BUDGET_TYPES[0]] = "0x4";
  assert.throws(() => parseManifest(JSON.stringify(budget)), /partial fleet budget/);
  for (const name of BUDGET_TYPES) budget.type_origins[name] = "0x4";
  parseManifest(JSON.stringify(budget));
  await verifyDeployment(manifestClient(budget), budget);
  const forgedBudget = structuredClone(budget); forgedBudget.type_origins[BUDGET_TYPES[0]] = "0x3";
  await assert.rejects(verifyDeployment(manifestClient(forgedBudget), budget), /type origin/);
  const settings = copy(); settings.type_origins[SETTINGS_TYPES[0]] = "0x4";
  assert.throws(() => parseManifest(JSON.stringify(settings)), /partial fleet settings/);
  for (const name of SETTINGS_TYPES) settings.type_origins[name] = "0x4";
  parseManifest(JSON.stringify(settings));
  await verifyDeployment(manifestClient(settings), settings);
  const forgedSettings = structuredClone(settings); forgedSettings.type_origins[SETTINGS_TYPES[0]] = "0x3";
  await assert.rejects(verifyDeployment(manifestClient(forgedSettings), settings), /type origin/);
  const missing = copy(); delete missing.type_origins[REQUIRED_TYPES[0]]; assert.throws(() => parseManifest(JSON.stringify(missing)));
  const incompatible = copy(); incompatible.supported_clients.dapp.max_exclusive = "0.0.1"; assert.throws(() => parseManifest(JSON.stringify(incompatible)));
  const oldRange = copy(); oldRange.supported_clients.dapp = { min: "0.0.1", max_exclusive: "0.0.2" };
  assert.throws(() => parseManifest(JSON.stringify(oldRange)), /unsupported dapp client version/);
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
