// SF-091 read-only recovery check against a real fullnode: from the public
// environment written by `fleet_recovery.py restore`, discover the fleet,
// verify health and check every indexed archive's ciphertext availability up
// to key release, then write the public-records export. No key, session,
// signature or transaction is used and plaintext is never requested, unless
// `--owner ADDRESS` is given: then the owner's BudgetOwnerCap is discovered in
// that wallet and archives are decrypted under the owner Seal policy with a
// five-minute session whose personal message is signed by the caller over
// stdin/stdout (as scripts/live_fleet_data.cjs). Only sequence, reason and
// record counts are reported; plaintext is wiped by the verified reader.
// Usage: node scripts/fleet_recovery_check.cjs RESTORED.env OUTPUT.json [--owner ADDRESS]
const fs = require('node:fs');
const readline = require('node:readline');
const [envFile, output, flag, ownerArgument] = process.argv.slice(2);
if (!envFile || !output || (flag && (flag !== '--owner' || !ownerArgument))) throw Error('usage: fleet_recovery_check.cjs RESTORED.env OUTPUT.json [--owner ADDRESS]');
for (const name of Object.keys(process.env)) if (name.startsWith('NEXT_PUBLIC_')) delete process.env[name];
for (const line of fs.readFileSync(envFile, 'utf8').split('\n').filter(Boolean)) {
  const at = line.indexOf('=');
  process.env[line.slice(0, at)] = JSON.parse(line.slice(at + 1));
}
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const load = loader();
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const { walrus, TESTNET_WALRUS_PACKAGE_CONFIG } = appRequire('@mysten/walrus');
const { requireManifest } = load('lib/deployment-manifest.ts');
const { DATA_CONFIG, fetchChainHealth, readDeviceData } = load('lib/fleet-data.ts');
const { archiveDecryptor } = load('lib/fleet-bulk.ts');
const { fetchBudgets } = load('lib/fleet-budget.ts');
const { SEAL_SETTINGS } = load('lib/seal.ts');
const recovery = load('lib/fleet-recovery.ts');
const { SessionKey } = appRequire('@mysten/seal');
const { isValidPersonalMessageSignature } = appRequire('@mysten/sui/verify');
const { normalizeSuiAddress: id, toBase64 } = appRequire('@mysten/sui/utils');

async function ownerDecryptor(client, owner) {
  if (!SEAL_SETTINGS) throw Error('restored Seal settings are required for policy access');
  const budget = (await fetchBudgets(client, owner)).find(b => id(b.id) === DATA_CONFIG.budgetId);
  if (!budget) throw Error("the wallet does not hold this fleet's BudgetOwnerCap");
  const session = await SessionKey.create({ address: owner, packageId: DATA_CONFIG.typeOrigin, ttlMin: 5, suiClient: client });
  console.log(JSON.stringify({ personal_message: toBase64(session.getPersonalMessage()) }));
  const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();
  const { signature } = JSON.parse((await lines.next()).value);
  if (!await isValidPersonalMessageSignature(session.getPersonalMessage(), signature, { address: owner })) throw Error('owner session signature failed verification');
  await session.setPersonalMessageSignature(signature);
  return { capId: budget.capId, decrypt: await archiveDecryptor(client, DATA_CONFIG, SEAL_SETTINGS, session, { kind: 'owner', ownerCapId: budget.capId }) };
}

(async () => {
  const manifest = requireManifest();
  if (manifest.network !== 'testnet' || !DATA_CONFIG) throw Error('this check reads the restored Testnet fleet only');
  const started = Date.now();
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: manifest.rpc_url });
  const storage = client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus;
  const devices = await recovery.discoverFleet(client, DATA_CONFIG);
  const discoveredMs = Date.now() - started;
  const health = await fetchChainHealth(client, DATA_CONFIG, devices);
  const owner = ownerArgument ? await ownerDecryptor(client, id(ownerArgument)) : undefined;
  const archives = {};
  for (const device of devices) {
    const writer = await readDeviceData(client, DATA_CONFIG, device);
    const count = Math.min(Number(writer.anchor_count), 32);
    archives[device] = [];
    for (let index = 0; index < count; index++) {
      const { state, recovery: action, verified, dependency, reason, archive } = await recovery.assessArchive(client, storage, DATA_CONFIG, device, index, manifest.walrus.aggregator_url, owner?.decrypt);
      archives[device].push({ index, state, recovery: action, ...(verified ? { verified } : {}), ...(dependency ? { dependency } : {}), reason,
        ...(archive ? { seq: archive.seq, archiveReason: archive.reason, records: archive.records.length } : {}) });
    }
  }
  const exported = await recovery.exportPublicRecords(client, DATA_CONFIG, devices);
  const { committee: { epoch } } = await storage.systemState();
  const result = { schema: 1, kind: 'suifleet-recovery-check', network: manifest.network, chainIdentifier: DATA_CONFIG.chainIdentifier,
    fleetId: DATA_CONFIG.fleetId, walrusEpoch: epoch, checkedAt: new Date(started).toISOString(), discoveredMs, totalMs: Date.now() - started,
    devices: devices.map(device => ({ device, health: health[device].status, archives: archives[device] })), publicExport: exported,
    ...(owner ? { ownerPolicy: { owner: id(ownerArgument), budgetOwnerCap: owner.capId, sessionMinutes: 5, threshold: SEAL_SETTINGS.threshold } } : {}),
    boundary: owner ? 'Read-only chain/Walrus reads plus owner-policy key release with a signed session; no transaction or plaintext recorded.'
      : 'Read-only fullnode/Walrus aggregator checks without Seal keys; policy decryption requires an owner/support signed session.' };
  fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify({ summary: { devices: devices.length, archives: Object.values(archives).flat().map(a => `${a.state}/${a.verified ?? '-'}`), discoveredMs, totalMs: result.totalMs } }));
  process.exit(0);
})().catch(error => { console.error(String(error).slice(0, 300)); process.exit(1); });
