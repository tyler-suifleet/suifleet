// Read-only public testnet profile verification. Never selects a wallet,
// submits a transaction, installs a profile or touches dapp/.env.local.
// node scripts/verify_seal_profile.cjs [PROFILE_DIRECTORY]
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
const { SealClient } = appRequire("@mysten/seal");
(async () => {
  const dir = path.resolve(process.argv[2] || path.join(__dirname, "../profiles/seal-testnet"));
  const load = loader(), dm = load("lib/deployment-manifest.ts"), seal = load("lib/seal.ts");
  const m = dm.parseManifest(fs.readFileSync(path.join(dir, "deployment.json"), "utf8"));
  const settings = JSON.parse(fs.readFileSync(path.join(dir, "seal.json"), "utf8"));
  assert.equal(m.network, "testnet"); assert.equal(m.chain_identifier, "4c78adac");
  assert.ok(dm.SEAL_POLICY_TYPES.every(name => m.type_origins[name]), "profile lacks Seal types");
  const parsed = seal.parseSealSettings(JSON.stringify(settings.keyServers), String(settings.threshold));
  const client = new SuiGrpcClient({ network: m.network, baseUrl: m.rpc_url });
  await dm.verifyDeployment(client, m);
  const { object: cap } = await client.getObject({ objectId: m.upgrade_cap_id });
  assert.equal(cap.owner.$kind, "AddressOwner");
  assert.equal(dm.normalizeId(cap.owner.AddressOwner), dm.normalizeId(settings.upgradeCapOwner));
  for (const server of parsed.keyServers) {
    const { object } = await client.getObject({ objectId: server.objectId });
    assert.equal(object.type, `${dm.normalizeId(settings.keyServerPackage)}::key_server::KeyServer`);
  }
  const servers = await new SealClient({ suiClient: client, serverConfigs: parsed.keyServers, verifyKeyServers: true }).getKeyServers();
  for (const server of servers.values()) {
    assert.notEqual(server.serverType, "Committee", "edge does not support committee providers");
    assert.equal(new URL(server.url).protocol, "https:");
  }
  // Ensure the separately supplied frontend environment really selects this
  // manifest/settings. The file contains public values only; never source it.
  const env = fs.readFileSync(path.join(dir, "frontend.env"), "utf8");
  const read = key => {
    const line = env.split("\n").find(l => l.startsWith(`${key}=`));
    assert.ok(line, `missing ${key}`);
    const value = line.slice(key.length + 1);
    return value.startsWith("'") && value.endsWith("'") ? value.slice(1, -1) : value;
  };
  assert.deepEqual(JSON.parse(read("NEXT_PUBLIC_DEPLOYMENT_MANIFEST")), m);
  assert.deepEqual(seal.parseSealSettings(read("NEXT_PUBLIC_SEAL_KEY_SERVERS"), read("NEXT_PUBLIC_SEAL_THRESHOLD")), parsed);
  console.log(JSON.stringify({ checked_at: new Date().toISOString(), network: m.network, chain: m.chain_identifier,
    package: m.call_package_id, registry: m.shared_objects.registry.id, upgrade_cap: m.upgrade_cap_id,
    namespace: m.type_origins["device_registry::DeviceRegistry"], threshold: parsed.threshold,
    key_servers: [...servers.keys()], checks: ["current frontend schema/client range", "fresh chain identity", "package version and per-type origins", "latest UpgradeCap and expected owner", "registry type and shared version", "Seal key server types and HTTPS proof of possession", "frontend environment matches profile"],
    limits: ["Read-only verification; no encrypted delivery or wallet acceptance", "Allow/deny/revoke delivery remains #32; final provider acceptance remains #159", "Testnet providers have no durability/SLA promise; reverify before use"] }, null, 2));
})().catch(error => { console.error(error.message); process.exitCode = 1; });
