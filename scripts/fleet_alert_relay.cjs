#!/usr/bin/env node
// Optional owner-run notification relay. Its output is never a browser trust
// input. Requires the same deployment/fleet-data config and locked SDKs as UI.
const fs=require('node:fs/promises'),path=require('node:path');
const {loader,appRequire}=require('./frontend_test_utils.cjs');
async function deliver(result,webhook,stateFile,fetcher=fetch) {
 if(result.status!=='ok')throw Error('Verified fleet data unavailable; incident state retained');
 const url=new URL(webhook);
 if(url.username || url.password || url.hash || (url.protocol!=='https:' && !(url.protocol==='http:' && ['localhost','127.0.0.1','[::1]'].includes(url.hostname))))throw Error('Use HTTPS for notifications');
 let previous=[];try{previous=JSON.parse(await fs.readFile(stateFile,'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;}
 if(!Array.isArray(previous) || previous.some(k=>typeof k!=='string') || previous.length>10000)throw Error('Invalid relay journal');
 const current=result.items.map(a=>a.key),newAlerts=result.items.filter(a=>!previous.includes(a.key)),resolved=previous.filter(k=>!current.includes(k));
 if(!newAlerts.length && !resolved.length)return;
 const response=await fetcher(url,{method:'POST',headers:{'content-type':'application/json',...(process.env.SUIFLEET_RELAY_TOKEN?{authorization:`Bearer ${process.env.SUIFLEET_RELAY_TOKEN}`}:{})},
   body:JSON.stringify({schema:1,observed_ms:result.serverNowMs,alerts:newAlerts,resolved}),signal:AbortSignal.timeout(30000)});
 if(!response.ok)throw Error('Notification delivery refused; journal unchanged');
 await fs.mkdir(path.dirname(stateFile),{recursive:true,mode:0o700});
 const file=await fs.open(stateFile+'.tmp','w',0o600);try{await file.writeFile(JSON.stringify(current));await file.sync();}finally{await file.close();}
 await fs.rename(stateFile+'.tmp',stateFile);const dir=await fs.open(path.dirname(stateFile),'r');try{await dir.sync();}finally{await dir.close();}
}
async function main() {
 const [owner,webhook,stateFile]=process.argv.slice(2);if(!owner || !webhook || !stateFile)throw Error('Usage: fleet_alert_relay.cjs OWNER WEBHOOK PRIVATE_STATE_FILE');
 const load=loader(),{DATA_CONFIG}=load('lib/fleet-data.ts'),{requireManifest}=load('lib/deployment-manifest.ts');
 if(!DATA_CONFIG)throw Error('Direct-chain fleet data configuration required');
 const manifest=requireManifest(),{SuiGrpcClient}=appRequire('@mysten/sui/grpc');
 const client=new SuiGrpcClient({network:manifest.network,baseUrl:manifest.rpc_url});
 const {fetchChainAlerts}=load('lib/chain-alerts.ts');
 do {
  try{await deliver(await fetchChainAlerts(client,owner),webhook,stateFile);}catch{console.error('Relay refresh/delivery unavailable; retained previous state');if(process.env.SUIFLEET_RELAY_ONCE==='1')process.exitCode=1;}
  if(process.env.SUIFLEET_RELAY_ONCE==='1')break;
  await new Promise(resolve=>setTimeout(resolve,60000));
 }while(true);
}
module.exports={deliver};
if(require.main===module)main().catch(()=>{console.error('Relay configuration failed');process.exitCode=1;});
