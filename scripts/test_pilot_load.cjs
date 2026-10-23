const assert = require('node:assert/strict');
const {validate,distribution,pool,retry} = require('./pilot_load.cjs');
const workload = require('../docs/validation/sf090/workload.json');
(async()=>{
 assert.equal(validate(structuredClone(workload)).devices,128);
 for(const edit of [{devices:0},{devices:1001},{concurrency:0},{concurrency:17},{rounds:0},{record_payload_bytes:[0]},{browser_devices:1},{browser_page_limit:101},{retry:{attempts:0}},{retry:{attempts:3,base_delay_ms:0,max_delay_ms:10}},{thresholds:{...workload.thresholds,verified_fetch_p95_ms:NaN}}])assert.throws(()=>validate({...structuredClone(workload),...edit}));
 assert.deepEqual(distribution([5,1,4,2,3]),{n:5,p50_ms:3,p95_ms:5,max_ms:5});
 assert.throws(()=>distribution([]));
 let active=0,peak=0,done=0;
 const measured=await pool(Array.from({length:37},(_,i)=>i),4,async()=>{active++;peak=Math.max(peak,active);await new Promise(r=>setTimeout(r,1));done++;active--;});
 assert.equal(done,37);assert.equal(peak,4);assert.equal(measured,4);
 let started=0,finished=0;await assert.rejects(pool(Array.from({length:20}),4,async()=>{started++;try{await new Promise(r=>setTimeout(r,1));throw Error('worker failed');}finally{finished++;}}),/worker failed/);assert.equal(started,4);assert.equal(finished,4);
 let attempts=0;const policy={attempts:3,base_delay_ms:1,max_delay_ms:2};
 await assert.rejects(retry(async()=>{attempts++;throw Error('integrity');},policy),/integrity/);assert.equal(attempts,1);
 attempts=0;assert.equal(await retry(async n=>{attempts++;if(n<3)throw Object.assign(Error('429'),{transient:true});return 'recovered';},policy),'recovered');assert.equal(attempts,3);
 attempts=0;await assert.rejects(retry(async()=>{attempts++;throw Object.assign(Error('outage'),{transient:true});},policy));assert.equal(attempts,3);
 const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),{writeReceipt}=require('./pilot_receipt.cjs');
 const temp=await fs.mkdtemp(path.join(os.tmpdir(),'pilot-receipt-test-'));
 try {
  const target=path.join(temp,'original'),output=path.join(temp,'receipt');await fs.writeFile(target,'preserve');await fs.symlink(target,output);
  await assert.rejects(writeReceipt(output,{result:'PASS'}),{code:'EEXIST'});assert.equal(await fs.readFile(target,'utf8'),'preserve');
  await fs.unlink(output);await writeReceipt(output,{result:'PASS'});
  await assert.rejects(writeReceipt(output,{result:'FAIL'}),{code:'EEXIST'});
  const handle=await fs.open(output,'r');
  try {assert.equal((await handle.stat()).mode&0o777,0o600);assert.equal(JSON.parse(await handle.readFile('utf8')).result,'PASS');}finally{await handle.close();}
 }finally{await fs.rm(temp,{recursive:true,force:true});}
 console.log('PASS receipt symlink/overwrite refusal, target preservation and private mode');
 console.log('PASS workload rejection, nearest-rank percentiles, bounded concurrency and retry exhaustion/recovery/nonretryable failures');
})().catch(e=>{console.error(e);process.exitCode=1;});
