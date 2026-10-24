#!/usr/bin/env node
// No daemon, API, token or signing key. Build locally and sign with your wallet.
const fs = require('node:fs');
async function main(argv = process.argv.slice(2)) {
  const [manifestFile, command, argument] = argv;
  if (!manifestFile || !command || argv.length > 3) throw Error('usage: suifleet_ops.cjs MANIFEST devices|groups|deployments|health|alerts OWNER\n       suifleet_ops.cjs MANIFEST build REQUEST.json\nBuild request: {command,sender,group,devices,...,out,maxGasMist}; output is unsigned, signing remains in your wallet.');
  const manifest = JSON.parse(fs.readFileSync(manifestFile));
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
  // Legacy HTTP build settings never select an operations backend.
  delete process.env.NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL;
  const { loader, appRequire } = require('./frontend_test_utils.cjs');
  const load = loader();
  const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
  const { normalizeSuiAddress: id, toBase64 } = appRequire('@mysten/sui/utils');
  const client = new SuiGrpcClient({ network: manifest.network, baseUrl: manifest.rpc_url });
  await load('lib/deployment-manifest.ts').verifyDeployment(client);
  const sui = load('lib/sui.ts');
  let result;
  if (command === 'build') {
    const request = JSON.parse(fs.readFileSync(argument));
    const { out, maxGasMist, ...operation } = request;
    if (!out || !Number.isSafeInteger(maxGasMist) || maxGasMist < 1 || maxGasMist > 500_000_000) throw Error('out and maxGasMist (1..500000000) required');
    const tx = await load('lib/fleet-ops.ts').buildOperation(client, operation);
    tx.setSender(id(operation.sender));
    tx.setGasBudget(maxGasMist);
    const bytes = await tx.build({ client });
    const output = { transaction: toBase64(bytes), digest: await tx.getDigest(), request: operation, maxGasMist, chainIdentifier: manifest.chain_identifier };
    fs.writeFileSync(out, JSON.stringify(output, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
    result = { out, digest: output.digest, unsigned: true };
  } else {
    const owner = id(argument);
    switch (command) {
      case 'devices': result = await sui.fetchDevices(client, owner); break;
      case 'groups': result = await sui.fetchGroups(client, owner); break;
      case 'deployments': result = await sui.fetchDeploymentEvents(client, owner); break;
      case 'health': {
        const devices = await sui.fetchDevices(client, owner);
        result = await load('lib/telemetry.ts').fetchFleetHealth(client, devices.map(d => d.objectId), owner); break;
      }
      case 'alerts': result = await load('lib/chain-alerts.ts').fetchChainAlerts(client, owner); break;
      default: throw Error('Unknown operations command');
    }
  }
  console.log(JSON.stringify(result));
  return result;
}
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { main };
