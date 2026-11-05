// Read-only bridge to the actual browser resolver, settings export and signed
// health verifier. No signing keys enter this process.
const fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(process.argv[2]));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(input.manifest);
process.env.NEXT_PUBLIC_FLEET_REPORTING_RELEASE=JSON.stringify(input.release);
const {loader,appRequire}=require('./frontend_test_utils.cjs'),load=loader();
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc');
(async()=>{
 const client=new SuiGrpcClient({network:'testnet',baseUrl:input.manifest.rpc_url});
 const reporting=load('lib/fleet-reporting.ts');
 const configs=[];
 for(const fleet of input.fleets) {
  const config=await reporting.resolveFleetReporting(client,fleet);
  if(!config)throw Error('Reporting missing');
  configs.push(config);
 }
 if(configs[0].fleetId===configs[1].fleetId||configs[0].storageId===configs[1].storageId)throw Error('Fleets not isolated');
 const settings=await reporting.reportingClientSettings(client,input.fleets[0],input.device);
 const health=(await load('lib/fleet-data.ts').fetchChainHealth(client,configs[0],[input.device]))[input.device];
 console.log(JSON.stringify({configs,settings,health}));
})().catch(e=>{console.error(String(e));process.exit(1)});
