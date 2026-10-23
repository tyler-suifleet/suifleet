const assert=require('node:assert/strict'),fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),http=require('node:http');
const {deliver}=require('./fleet_alert_relay.cjs');
(async()=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'sf082-relay-')),state=path.join(dir,'state.json');let fail=true,calls=[];
 const server=http.createServer(async(req,res)=>{let body='';for await(const c of req)body+=c;calls.push(JSON.parse(body));res.writeHead(fail?503:200);res.end();});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const url=`http://127.0.0.1:${server.address().port}/alerts`;
 try{
  const result={status:'ok',items:[{key:'device-offline:0x1:',kind:'device-offline'}],serverNowMs:100};
  await assert.rejects(deliver(result,url,state));await assert.rejects(fs.readFile(state),{code:'ENOENT'});
  fail=false;await deliver(result,url,state);assert.equal(calls.length,2);
  await deliver(result,url,state);assert.equal(calls.length,2,'confirmed notifications deduplicate across reads');
  await assert.rejects(deliver({status:'unavailable'},url,state));assert.equal((JSON.parse(await fs.readFile(state))).length,1,'outage does not resolve incidents');
  await deliver({...result,items:[]},url,state);assert.deepEqual(calls.at(-1).resolved,['device-offline:0x1:']);
  assert.deepEqual(JSON.parse(await fs.readFile(state)),[]);
  assert.equal((await fs.stat(state)).mode&0o777,0o600);
  console.log('Owner relay retries delivery, persists deduplication and resolves only verified incident changes');
 }finally{await new Promise(resolve=>server.close(resolve));await fs.rm(dir,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exit(1)});
