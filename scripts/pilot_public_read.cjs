const {writeReceipt}=require('./pilot_receipt.cjs');
// Read-only bounded Testnet sample. No wallet, signing API or faucet is loaded.
const assert=require('node:assert/strict'),fs=require('node:fs'),{performance}=require('node:perf_hooks'),{execFileSync}=require('node:child_process');
const {appRequire}=require('./frontend_test_utils.cjs'),{SuiGrpcClient}=appRequire('@mysten/sui/grpc'),{fromBase58,toHex}=appRequire('@mysten/sui/utils');
const {pool,distribution}=require('./pilot_load.cjs');
(async()=>{
 fs.rmSync(process.argv[2]||'/tmp/sf090-public-read.json',{force:true});
 const workload=require('../docs/validation/sf090/public-read-workload.json');assert.equal(workload.chain,'4c78adac');assert.equal(workload.writes,0);assert.equal(workload.new_spend_ceiling_mist,0);assert.equal(workload.max_digests,8);assert.equal(workload.concurrency,2);
 const client=new SuiGrpcClient({network:'testnet',baseUrl:workload.rpc});
 const chain=toHex(fromBase58((await client.getChainIdentifier()).chainIdentifier).slice(0,4));assert.equal(chain,workload.chain);
 const history=require('../docs/validation/sf082-device-effects.json');assert.equal(history.chain,chain);
 const sample=history.transactions.filter(t=>t.status.success&&t.targets.some(target=>/::(report_health|anchor|register_blob|certify_blob)$/.test(target))).slice(-workload.max_digests);assert.equal(sample.length,workload.max_digests);
 const results=[];
 const peak=await pool(sample,workload.concurrency,async expected=>{
  const start=performance.now(),r=await client.getTransaction({digest:expected.digest,include:{effects:true}});
  assert(r.Transaction?.status.success);assert.equal(r.Transaction.digest,expected.digest);
  const gas=r.Transaction.effects.gasUsed,net=(BigInt(gas.computationCost)+BigInt(gas.storageCost)-BigInt(gas.storageRebate)).toString();assert.equal(net,String(expected.net_gas_mist));
  results.push({digest:expected.digest,targets:expected.targets,status:'success',historical_net_gas_mist:net,read_ms:performance.now()-start});
 });
 const timing=distribution(results.map(r=>r.read_ms));assert(timing.p95_ms<workload.read_p95_ms);
 const receipt={schema:1,result:'PASS',mode:'fresh-public-testnet-historical-effects-readback',revision:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),observed_at:new Date().toISOString(),chain,rpc:workload.rpc,peak_concurrency:peak,measurements:timing,results,new_spend_mist:'0',writes:0,limits:['historical producer effects independently re-read; not new SF-090 native transactions','no upload/keyserver latency or availability measurement','no production capacity claim']};
 await writeReceipt(process.argv[2]||'/tmp/sf090-public-read.json',receipt);console.log(JSON.stringify({result:receipt.result,measurements:timing,new_spend_mist:'0'}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
