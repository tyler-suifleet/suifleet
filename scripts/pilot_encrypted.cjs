// Integrated synthetic-device stage on the disposable SF-090 chain. Local test
// providers implement Seal sessions and real on-chain approval, not production quotas.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const {spawn, execFileSync} = require('node:child_process');
const {randomBytes, createHash} = require('node:crypto');
const {appRequire, loader} = require('./frontend_test_utils.cjs');
const {Transaction} = appRequire('@mysten/sui/transactions');
const {SealClient, SessionKey, EncryptedObject} = appRequire('@mysten/seal');
const {pool, retry} = require('./pilot_load.cjs');
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
async function listen(server) {
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
 return server.address().port;
}
async function start({client,rpc,core,registry,devices,owner,execute,created,publish,keyDirectory,tmp,workload,measure}) {
 const children=[],servers=[],providerSpecs=[]; let gateway;
 async function close() {
  for(const child of children) if(child.exitCode===null && child.signalCode===null) {child.kill();await new Promise(resolve=>child.once('exit',resolve));}
  if(gateway)await new Promise(resolve=>gateway.close(resolve));
 }
 try {
  const binary='suifleet-edge/target/debug/examples/seal_key_server';
  async function launch({objectId,seed,port}) {
  const child=spawn(binary,['serve',rpc,core,objectId,seed,String(port)],{stdio:['ignore','pipe','ignore']});children.push(child);
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Seal test server startup timeout')),10000);child.once('error',e=>{clearTimeout(timer);reject(e);});child.once('exit',()=>{clearTimeout(timer);reject(Error('Seal test server exited'));});child.stdout.once('data',data=>{clearTimeout(timer);String(data).startsWith('listening')?resolve():reject(Error('Seal test server not ready'));});});

  return child;
  }
  const kp=await publish('test Seal server objects',keyDirectory);
  for(let i=0;i<3;i++) {
   const seed=randomBytes(32).toString('hex');
   const pk=execFileSync(binary,['public-key',seed],{encoding:'utf8'}).trim();
   const reservation=http.createServer(); const port=await listen(reservation);await new Promise(resolve=>reservation.close(resolve));
   const tx=new Transaction();tx.moveCall({target:`${kp.pkg}::key_server::register`,arguments:[tx.pure.string(`pilot-${i}`),tx.pure.string(`http://127.0.0.1:${port}`),tx.pure.vector('u8',Buffer.from(pk,'hex'))]});
   const objectId=created(await execute(`register test Seal server ${i}`,tx),'::key_server::KeyServer');
   providerSpecs.push({objectId,seed,port});await launch(providerSpecs.at(-1));
   servers.push({objectId,weight:1});
  }
  let tx=new Transaction();const [cap]=tx.moveCall({target:`${core}::seal_policy::create_policy`,arguments:[tx.pure.string('pilot encrypted release')]});tx.transferObjects([cap],owner.toSuiAddress());
  const policyResult=await execute('create encrypted pilot policy',tx),policy=created(policyResult,'::seal_policy::AccessPolicy'),admin=created(policyResult,'::seal_policy::AccessAdminCap');
  tx=new Transaction();for(const d of devices)tx.moveCall({target:`${core}::seal_policy::grant_device`,arguments:[tx.object(policy),tx.object(admin),tx.object(registry),tx.pure.id(d.deviceId)]});await execute('authorize enrolled pilot devices',tx);
  const seal=loader()('lib/seal.ts');
  const artifact=randomBytes(workload.artifact_bytes),artifactHash=digest(artifact);
  const recipe=Buffer.from(JSON.stringify({schema_version:1,artifact_sha256:artifactHash,operation:'synthetic file apply'})),recipeHash=digest(recipe);
  const identity=seal.newIdentity(policy,0),encrypt=seal.sealer(client,{keyServers:servers,threshold:2},core,identity,true);
  const objects={artifact:await encrypt(artifact,'artifact',artifactHash),recipe:await encrypt(recipe,'recipe',recipeHash)};
  let throttled=false,requests=0; const applies=new Map();
  gateway=http.createServer((req,res)=>{requests++;if(throttled){res.writeHead(429);res.end();return;}const body=objects[req.url.slice(1)];if(!body){res.writeHead(404);res.end();return;}res.setHeader('Content-Length',body.length);res.end(body);});
  const port=await listen(gateway);
  async function open(d,kind,policyId=policy) {
   const response=await fetch(`http://127.0.0.1:${port}/${kind}`,{signal:AbortSignal.timeout(10000)});
   if(response.status===429)throw Object.assign(Error('gateway throttled'),{transient:true});
   assert(response.ok);const envelope=Buffer.from(await response.arrayBuffer());assert.equal(digest(envelope),digest(objects[kind]));
   const data=envelope.subarray(seal.MAGIC.length),parsed=EncryptedObject.parse(data);
   const session=await SessionKey.create({address:d.key.toSuiAddress(),packageId:core,ttlMin:10,signer:d.key,suiClient:client});
   const approval=new Transaction();approval.moveCall({target:`${core}::seal_policy::seal_approve_device`,arguments:[approval.pure.vector('u8',Buffer.from(parsed.id,'hex')),approval.object(policyId),approval.object(registry),approval.pure.id(d.deviceId)]});
   const txBytes=await approval.build({client,onlyTransactionKind:true});
   const sdk=new SealClient({suiClient:client,serverConfigs:servers,verifyKeyServers:true});
   const plaintext=await sdk.decrypt({data,sessionKey:session,txBytes,checkShareConsistency:true});
   assert.deepEqual(Buffer.from(parsed.ciphertext.Aes256Gcm.aad),Buffer.from(seal.aad(kind,kind==='artifact'?artifactHash:recipeHash)));
   assert.equal(digest(plaintext),kind==='artifact'?artifactHash:recipeHash);return plaintext;
  }
  // Fresh sessions, fresh SDK instances and actual denied Move approval: no DEM
  // key side channel or cached decrypt success can satisfy this control.
  const outsider={key:appRequire('@mysten/sui/keypairs/ed25519').Ed25519Keypair.generate(),deviceId:devices[0].deviceId};
  await assert.rejects(open(outsider,'artifact'));
  throttled=true;let attempts=0;
  await assert.rejects(retry(()=>{attempts++;return open(devices[0],'artifact');},workload.retry));assert.equal(attempts,workload.retry.attempts);assert.equal(applies.size,0);
  throttled=false;
  // Two missing shares cannot meet threshold two. Use fresh SDKs/sessions,
  // then restart the same local providers and require later applies to recover.
  for(const child of children.slice(0,2)) {child.kill();await new Promise(resolve=>child.once('exit',resolve));}
  await assert.rejects(open(devices[0],'artifact'));assert.equal(applies.size,0);
  for(const spec of providerSpecs.slice(0,2))await launch(spec);
  return {artifactHash,recipeHash,blobId:'isolated-encrypted-pilot',close,
   async apply(d) {
    if(applies.has(d.deviceId))return; // one process-local synthetic application, not daemon restart recovery
    const [a,r]=await measure('encrypted_fetch',()=>Promise.all([open(d,'artifact'),open(d,'recipe')]));
    assert.equal(JSON.parse(Buffer.from(r)).artifact_sha256,digest(a));
    const destination=`${tmp}/applied-${d.deviceId}`;await fs.writeFile(destination,a,{flag:'wx',mode:0o600});assert.equal(digest(await fs.readFile(destination)),artifactHash);applies.set(d.deviceId,1);
   },
   async verify() {
    assert.equal(applies.size,devices.length);for(const n of applies.values())assert.equal(n,1);
    return {devices:applies.size,artifact_bytes:artifact.length,recipe_bytes:recipe.length,gateway_requests:requests,throttle_attempts:attempts,unauthorized_decryption:'refused',threshold_outage:'refused_then_recovered',applies_per_device:1,
     key_server_binary_sha256:digest(await fs.readFile(binary)),limits:['synthetic file apply, not native updater','local test Seal providers and gateway; no production quotas or Walrus certification throughput']};
   }};
 } catch(error) {await close();throw error;}
}
module.exports={start};
