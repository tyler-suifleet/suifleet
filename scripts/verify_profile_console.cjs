// Read-only smoke test of the console's connected-account queries. No signer.
// node scripts/verify_profile_console.cjs PROFILE_DIRECTORY PUBLIC_OWNER_ADDRESS
const path = require('node:path');
const fs = require('node:fs');
const dir = path.resolve(process.argv[2] || 'profiles/seal-testnet');
const owner = process.argv[3];
if (!/^0x[0-9a-fA-F]{64}$/.test(owner || '')) throw Error('Supply a public owner address; no wallet credentials are needed');
// Set the public manifest before loading application modules that capture it.
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = fs.readFileSync(path.join(dir, 'deployment.json'), 'utf8');
const { checkProfile } = require('./check_profiles.cjs');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const load = loader();
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
(async () => {
  let client;
  const profile = await checkProfile(dir, m => (client = new SuiGrpcClient({ network: m.network, baseUrl: m.rpc_url })));
  const sui = load('lib/sui.ts'), catalog = load('lib/catalog.ts'), enrollment = load('lib/enrollment.ts');
  const balance = await client.getBalance({ owner, coinType: '0x2::sui::SUI' });
  const devices = await sui.fetchDevices(client, owner);
  const catalogs = await catalog.fetchCatalogs(client, owner);
  const deployments = await sui.fetchDeploymentEvents(client, owner);
  const policies = await enrollment.fetchPolicies(client, owner);
  console.log(JSON.stringify({ profile, owner, balance: String(balance.balance.balance), devices: devices.length,
    catalogs: catalogs.length, deployments: deployments.length, enrollment_policies: policies.length,
    limits: 'Application read functions only; no browser wallet connection, signatures, writes or production provider acceptance' }, null, 2));
})().catch(error => { console.error(error.message); process.exitCode = 1; });
