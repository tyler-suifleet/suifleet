// Read-only SF-190 repro. Usage: node scripts/test_storage_errors_testnet.cjs PROFILE_DIRECTORY [RECEIPT]
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const {appRequire,loader}=require(root+'/scripts/frontend_test_utils.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict');
assert(process.argv[2], 'provide the evaluation Testnet profile directory');
const profile=path.resolve(process.argv[2])+'/';
const receipt=process.argv[3] || path.join(fs.mkdtempSync(path.join(require('node:os').tmpdir(),'sf190-testnet-')), 'receipt.json');
const manifest=JSON.parse(fs.readFileSync(profile+'deployment.json'));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc');
const {toHex,fromBase58}=appRequire('@mysten/sui/utils');
(async()=>{
const client=new SuiGrpcClient({network:'testnet',baseUrl:manifest.rpc_url});
assert.equal(toHex(fromBase58((await client.getChainIdentifier()).chainIdentifier).slice(0,4)),'4c78adac');
const load=loader(),schema=load('lib/fleet-protocol.ts');
const events=await client.listEvents({filter:{eventType:manifest.type_origins['fleet_budget::BudgetCreated']+'::fleet_budget::BudgetCreated'},limit:100});
const budgets=events.events.map(e=>schema.fleet_budget_BudgetCreatedBcs.parse(e.bcs));
const budget=budgets.find(b=>b.budget_id.startsWith('0x1843'));
assert(budget,'repro budget must be present');
const config=JSON.parse(fs.readFileSync(profile+'fleet-storage.json'));
const storage=await load('lib/fleet-budget.ts').fetchStorage(client,config,budget.budget_id);
assert.equal(storage,undefined);
fs.writeFileSync(receipt,JSON.stringify({chain:'4c78adac',budget:budget.budget_id,storage_package:config.packageId,result:'PASS: actual storage reader returns absent for repro budget',writes:0,limits:['read-only Testnet API check; rendered empty state covered by component regression, no manual wallet/browser walkthrough']},null,2),{flag:'wx',mode:0o600});
console.log(fs.readFileSync(receipt,'utf8'));
})().catch(e=>{console.error(e);process.exitCode=1});
