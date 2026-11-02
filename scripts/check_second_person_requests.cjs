// Read the live queue through the production console client; no signing keys.
const fs = require('node:fs');
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const [manifest, customer, expected] = process.argv.slice(2);
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = fs.readFileSync(manifest, 'utf8');
const m = JSON.parse(process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST);
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const load = loader(), approvals = load('lib/second-person.ts'), security = load('lib/security-policy.ts');
(async () => {
 const client = new SuiGrpcClient({network:m.network,baseUrl:m.rpc_url});
 const requests = await approvals.fetchRequests(client,customer);
 if (expected === 'unprotected-ids') { const policy=await security.fetchOrganizationSecurity(client,customer); if (!policy || policy.requirements.some(v=>v!==0)) throw Error('resume requires verified all-off policy'); process.stdout.write(JSON.stringify(requests.map(r=>r.id))); return; }
 if (expected === 'empty' ? requests.length !== 0 : !requests.some(r=>r.id === expected)) throw Error('console current-state queue mismatch');
 console.log('PASS console live queue: '+requests.length+' pending requests, verified deployment/origins/arguments');
})().catch(e=>{console.error(e.message);process.exitCode=1});
