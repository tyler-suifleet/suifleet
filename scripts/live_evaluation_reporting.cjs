// Actual application builders/resolvers; public inputs only. Keys stay in Python's wallet runtime.
const fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(process.argv[2]));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(input.manifest);
process.env.NEXT_PUBLIC_FLEET_REPORTING_RELEASE=JSON.stringify(input.release);
const {loader,appRequire}=require('./frontend_test_utils.cjs'),load=loader();
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc');
(async()=>{
 const client=new SuiGrpcClient({network:'testnet',baseUrl:input.manifest.rpc_url});
 const reporting=load('lib/fleet-reporting.ts'), fleet=input.fleet;
 if(input.action==='setup'||input.action==='enroll'){
  const config=input.action==='enroll'?await reporting.resolveFleetReporting(client,fleet):undefined;
  const tx=input.action==='setup'?await reporting.buildProvisionReporting(client,fleet):await reporting.buildEnrollReporting(client,fleet,config,input.device);
  const budget=await load('lib/fleet-budget.ts').fetchManagedBudget(client,fleet.budgetId);
  const wrapped=load('lib/customer-fleets.ts').withManagedBudgetCap(tx,fleet,budget.capId);
  wrapped.setSender(input.sender);wrapped.setGasBudget(input.gasBudget);
  console.log(JSON.stringify({bytes:Buffer.from(await wrapped.build({client})).toString('base64')}));return;
 }
 const config=await reporting.resolveFleetReporting(client,fleet);
 if(!config)throw Error('Reporting missing');
 const storageRelease=await reporting.resolveFleetStorageRelease(client,fleet);
 const storage=await load('lib/fleet-budget.ts').fetchStorage(client,storageRelease.config,fleet.budgetId);
 if(!storage||storage.id!==config.storageId)throw Error('Reporting storage not discoverable by normal funding reader');
 const settings=await reporting.reportingClientSettings(client,fleet,input.device);
 const health=(await load('lib/fleet-data.ts').fetchChainHealth(client,config,[input.device]))[input.device];
 console.log(JSON.stringify({config,settings,health,storageRelease,storageId:storage.id}));
})().catch(e=>{console.error(String(e));process.exit(1)});
