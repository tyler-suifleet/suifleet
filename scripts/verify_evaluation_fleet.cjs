// Read-only verification of the hosted Testnet evaluation fleet (SF-173).
// Loads the profile's public frontend.env into the dApp's own modules and
// checks, with the same code the console runs, that every fleet feature is
// configured: fleet budget, fleet settings, prepaid Walrus storage and the
// fleet-data/health index. Then reads the evaluation fleet's objects through
// the dApp's readers. Never selects a wallet or submits a transaction.
// node scripts/verify_evaluation_fleet.cjs [PROFILE_DIRECTORY]
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");

function readEnv(file) {
  const values = {};
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    if (!line || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    assert.ok(at > 0 && line.startsWith("NEXT_PUBLIC_"), `unexpected frontend.env line: ${line.slice(0, 40)}`);
    const raw = line.slice(at + 1);
    values[line.slice(0, at)] = raw.startsWith("'") && raw.endsWith("'") ? raw.slice(1, -1) : raw;
  }
  return values;
}

(async () => {
  const dir = path.resolve(process.argv[2] || path.join(__dirname, "../profiles/seal-testnet"));
  const json = name => JSON.parse(fs.readFileSync(path.join(dir, name), "utf8"));
  const env = readEnv(path.join(dir, "frontend.env"));
  // The console reads these at module load; set them before loading anything.
  for (const [key, value] of Object.entries(env)) process.env[key] = value;
  const load = loader();
  const dm = load("lib/deployment-manifest.ts"), budget = load("lib/fleet-budget.ts"), settings = load("lib/fleet-settings.ts");
  const settingsTx = load("lib/fleet-settings-tx.ts"), data = load("lib/fleet-data.ts");
  const record = json("evaluation-fleet.json"), storageFile = json("fleet-storage.json"), dataFile = json("fleet-data.json");
  const m = dm.MANIFEST;
  assert.deepEqual(m, dm.parseManifest(fs.readFileSync(path.join(dir, "deployment.json"), "utf8")), "frontend.env selects another manifest");
  assert.equal(m.network, "testnet"); assert.equal(m.chain_identifier, "4c78adac");
  assert.equal(dm.normalizeId(record.registry), dm.normalizeId(m.shared_objects.registry.id));

  // Each gate behind a "not configured"/"not available" message in the console.
  assert.ok(budget.budgetSupported(), "Network budget: no fleet budget types (\"Shared fleet funding is not available\")");
  assert.ok(settings.settingsSupported(), "Fleet settings: no settings types (\"not available in this fleet configuration\")");
  const storage = budget.storageConfig();
  assert.ok(storage.config, `Data/publishing: ${storage.reason}`);
  assert.equal(storage.config.packageId, dm.normalizeId(storageFile.packageId));
  assert.equal(storage.config.storageOrigin, dm.normalizeId(storageFile.storageOrigin));
  assert.equal(load("lib/retention.ts").retentionOrigin(), dm.normalizeId(storageFile.retentionOrigin));
  assert.ok(data.DATA_CONFIG, "Device services: no fleet data (\"not connected to the reporting service\")");
  assert.deepEqual(data.DATA_CONFIG, data.parseDataConfig(JSON.stringify(dataFile)));
  const saver = settingsTx.settingsSaver({ id: record.objects.budget, capId: record.objects.budget_cap });
  assert.ok(saver.build && !saver.reason, `Fleet settings: ${saver.reason}`);

  const client = new SuiGrpcClient({ network: m.network, baseUrl: m.rpc_url });
  await dm.verifyDeployment(client, m);
  const budgets = await budget.fetchBudgets(client, record.operator);
  const view = budgets.find(b => dm.normalizeId(b.id) === dm.normalizeId(record.objects.budget));
  assert.ok(view, "the evaluation fleet budget is not the operator's");
  assert.equal(dm.normalizeId(view.capId), dm.normalizeId(record.objects.budget_cap));
  const saved = await settings.fetchFleetSettings(client, view.id);
  assert.ok(saved, "the evaluation fleet budget has no saved settings");
  const stored = await budget.fetchStorage(client, storage.config, view.id);
  assert.ok(stored && dm.normalizeId(stored.id) === dm.normalizeId(record.objects.storage), "fleet storage not found for the budget");
  const fleet = await data.readFleetData(client, data.DATA_CONFIG);
  const result = {
    checked_at: new Date().toISOString(), network: m.network, chain: m.chain_identifier, package: m.call_package_id,
    package_version: m.package_version, registry: m.shared_objects.registry.id,
    fleet_storage_package: storage.config.packageId, fleet_data_package: data.DATA_CONFIG.packageId,
    budget: { id: view.id, balance_mist: String(view.balance), devices: String(view.deviceCount), paused: view.paused },
    settings: Object.fromEntries(Object.entries(saved).map(([k, v]) => [k, String(v)])),
    storage: { id: stored.id, wal_frost: String(stored.walBalance), pool_bytes: String(stored.poolSize), end_epoch: stored.endEpoch ?? null },
    fleet_data: { id: data.DATA_CONFIG.fleetId, devices: fleet.device_count, health_interval_ms: fleet.health_interval_ms,
      batch_interval_ms: fleet.batch_interval_ms, emergency_interval_ms: fleet.emergency_interval_ms },
    checks: ["frontend.env selects deployment.json, fleet-storage.json and fleet-data.json", "budgetSupported/settingsSupported/storageConfig/DATA_CONFIG/settingsSaver all enabled in the dApp's own modules",
      "fresh chain identity, package version, per-type origins and UpgradeCap", "operator-owned budget and cap, saved settings, fleet storage and FleetData bindings read through the dApp's readers"],
    limits: ["Read-only; no browser/wallet session", "Health appears only after an enrolled device daemon reports (physical/target acceptance stays in #32)"],
  };
  console.log(JSON.stringify(result, null, 2));
})().catch(error => { console.error(error.message); process.exitCode = 1; });
