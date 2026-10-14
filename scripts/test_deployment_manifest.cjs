// Run the actual TypeScript validator without introducing a second test framework.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const ts = require("../dapp/node_modules/typescript");
const Module = require("node:module");
const filename = path.resolve(__dirname, "../dapp/src/lib/deployment-manifest.ts");
const fixture = JSON.parse(fs.readFileSync(process.argv[2] ?? path.resolve(__dirname, "../manifests/fixture.localnet.json"), "utf8"));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true } }).outputText;
const loaded = new Module(filename, module);
loaded.filename = filename;
loaded.paths = Module._nodeModulePaths(path.dirname(filename));
loaded._compile(source, filename);
const { parseManifest, verifyDeployment, moveType, REQUIRED_TYPES } = loaded.exports;
const copy = () => structuredClone(fixture);
const client = (live = fixture) => ({
  getChainIdentifier: async () => live.chain_identifier,
  getObject: async ({id}) => id === live.call_package_id ? {data: {version:live.package_version, bcs:{dataType:"package", typeOriginTable:Object.entries(live.type_origins).map(([name, origin]) => ({module_name:name.split("::")[0], datatype_name:name.split("::")[1], package:origin}))}}} :
    id === live.upgrade_cap_id ? {data:{content:{dataType:"moveObject",type:"0x2::package::UpgradeCap",fields:{package:live.call_package_id,version:live.package_version}}}} :
    {data: {owner:{Shared:{initial_shared_version:"1"}}, content:{dataType:"moveObject", type: `${live.type_origins["device_registry::DeviceRegistry"]}::device_registry::DeviceRegistry`}}},
});
(async () => {
  assert.deepEqual(parseManifest(JSON.stringify(fixture)), fixture);
  if (process.argv[2]) {
    const { SuiClient } = require("../dapp/node_modules/@mysten/sui/client");
    await verifyDeployment(new SuiClient({ url: fixture.rpc_url }), fixture);
    console.log("PASS: actual frontend SDK validates live multi-upgrade package, UpgradeCap and registry");
    return;
  }
  assert.equal(moveType("deployment_manager::RecipeDigestKey"), `${fixture.type_origins["deployment_manager::RecipeDigestKey"]}::deployment_manager::RecipeDigestKey`);
  assert.equal(moveType("device_cap::DeviceAddressKey"), `${fixture.type_origins["device_cap::DeviceAddressKey"]}::device_cap::DeviceAddressKey`);
  await verifyDeployment(client(), fixture);
  for (const [key,value] of [["schema_version",2],["protocol_version",2],["call_package_id","bad"],["network","unknown"]]) {
    const invalid=copy(); invalid[key]=value; assert.throws(() => parseManifest(JSON.stringify(invalid)));
  }
  const missing=copy(); delete missing.type_origins[REQUIRED_TYPES[0]]; assert.throws(() => parseManifest(JSON.stringify(missing)));
  const incompatible=copy(); incompatible.supported_clients.dapp.max_exclusive="0.0.1"; assert.throws(() => parseManifest(JSON.stringify(incompatible)));
  const staleChain=copy(); staleChain.chain_identifier="deadbeef";
  await assert.rejects(verifyDeployment(client(staleChain), fixture), /chain identifier/);
  const staleOrigin=copy(); staleOrigin.type_origins["deployment_manager::RecipeDigestKey"] = staleOrigin.call_package_id;
  await assert.rejects(verifyDeployment(client(staleOrigin), fixture), /type origin/);
  const staleCap = client(); const capOriginal = staleCap.getObject;
  staleCap.getObject = async (args) => args.id === fixture.upgrade_cap_id ? {data:{content:{dataType:"moveObject",type:"0x2::package::UpgradeCap",fields:{package:"0x4",version:"4"}}}} : capOriginal(args);
  await assert.rejects(verifyDeployment(staleCap, fixture), /UpgradeCap/);
  const staleVersion=copy(); staleVersion.package_version="2";
  await assert.rejects(verifyDeployment(client(staleVersion), fixture), /package metadata/);
  const staleRegistry = client(); staleRegistry.getObject = async () => ({data:{owner:null,content:{dataType:"moveObject",type:`${fixture.type_origins["device_registry::DeviceRegistry"]}::device_registry::DeviceRegistry`}}});
  // Preserve the package response to exercise registry failure, not package failure.
  const original = client(); const registryRead = staleRegistry.getObject;
  staleRegistry.getObject = async (args) => args.id === fixture.shared_objects.registry.id ? registryRead(args) : original.getObject(args);
  await assert.rejects(verifyDeployment(staleRegistry, fixture), /registry/);
  console.log("PASS: frontend manifest, mixed origins, stale chain/package/types/registry and incompatible clients");
})().catch((error) => { console.error(error); process.exitCode = 1; });
