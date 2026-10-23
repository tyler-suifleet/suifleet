// SF-090: real crypto/consumer code over explicitly synthetic chain/provider I/O.
// No keys or plaintext are written to the receipt. This submits no transactions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash, createDecipheriv, randomBytes } = require('node:crypto');
const { performance } = require('node:perf_hooks');
const { execFileSync } = require('node:child_process');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { normalizeSuiAddress: id, toBase58 } = appRequire('@mysten/sui/utils');
const { bcs } = appRequire('@mysten/sui/bcs');
const { SealClient, EncryptedObject, DemType } = appRequire('@mysten/seal');
const vectors = require('../interfaces/seal-vectors.json');
const manifest = structuredClone(require('../manifests/fixture.localnet.json'));
manifest.chain_identifier = '4c78adac'; manifest.shared_objects.registry.id = id('0xa1');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const load = loader(), data = load('lib/fleet-data.ts'), bulk = load('lib/fleet-bulk.ts');
const protocol = load('lib/fleet-protocol.ts'), budget = load('lib/fleet-budget.ts');
const config = data.parseDataConfig(JSON.stringify({ schema: 1, chainIdentifier: manifest.chain_identifier,
 registryId: id('0xa1'), packageId: id('0xa2'), typeOrigin: id('0xa2'), fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0xb2') }));
const storageOrigin = id('0xb3');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function validate(w) {
 for (const k of ['devices', 'concurrency', 'rounds', 'browser_devices', 'browser_page_limit']) assert(Number.isSafeInteger(w[k]) && w[k] > 0, k);
 assert(w.devices <= 1000 && w.concurrency <= 16 && w.concurrency <= w.devices && w.rounds <= 10);
 assert.equal(w.browser_devices, w.devices); assert.equal(w.browser_page_limit, 100);
 assert(Array.isArray(w.record_payload_bytes) && w.record_payload_bytes.length === w.rounds);
 for (const n of w.record_payload_bytes) assert(Number.isSafeInteger(n) && n >= 1024 && n <= 1048576);
 assert.equal(w.logs_per_archive, 1024);
 assert(Number.isSafeInteger(w.retry.attempts) && w.retry.attempts >= 1 && w.retry.attempts <= 5);
 assert(Number.isSafeInteger(w.retry.base_delay_ms) && w.retry.base_delay_ms > 0 && w.retry.base_delay_ms <= w.retry.max_delay_ms && w.retry.max_delay_ms <= 1000);
 for (const k of ['sign_encrypt_p95_ms','verified_fetch_p95_ms','health_page_p95_ms','index_page_p95_ms','peak_rss_bytes','max_attempts_per_operation']) assert(Number.isFinite(w.thresholds[k]) && w.thresholds[k] > 0, k);
 assert.equal(w.thresholds.unexpected_failures, 0);
 assert(w.retry.attempts <= w.thresholds.max_attempts_per_operation);
 return w;
}
function distribution(values) {
 assert(values.length); const v = [...values].sort((a,b) => a-b);
 return { n: v.length, p50_ms: v[Math.ceil(v.length*.5)-1], p95_ms: v[Math.ceil(v.length*.95)-1], max_ms: v.at(-1) };
}
async function pool(items, concurrency, fn) {
 let next = 0, active = 0, peak = 0, failure;
 await Promise.all(Array.from({length: Math.min(concurrency, items.length)}, async () => {
  while (!failure && next < items.length) { const item = items[next++]; active++; peak = Math.max(peak, active);
   try { await fn(item); } catch(error) { failure ??= error; } finally { active--; }
  }
 })); if(failure)throw failure; return peak;
}
async function retry(fn, policy, onAttempt = () => {}) {
 for (let attempt = 1; ; attempt++) {
  onAttempt(attempt);
  try { return await fn(attempt); } catch (e) {
   // Only explicit transient test transport failures are retryable. Integrity,
   // binding, authorization and exhausted-budget failures must fail immediately.
   if (!e.transient || attempt >= policy.attempts) throw e;
   await sleep(Math.min(policy.max_delay_ms, policy.base_delay_ms * 2 ** (attempt-1)));
  }
 }
}
function transient(label) { const e = Error(label); e.transient = true; return e; }
async function run(w) {
 validate(w);
 const revision=execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim();
 const now = Date.now(), entries = [], byDevice = new Map(), byWriter = new Map();
 const stats = {}, faults = [], byPayload = [], cpuStart = process.cpuUsage(), started = performance.now();
 let rpcFault = false, gatewayFault = false, keyFault = false, delayed = false, keyRequests = 0, peakConcurrency = 0;
 const measured = async (name, fn) => { const t = performance.now(); const r = await fn(); (stats[name] ??= []).push(performance.now()-t); return r; };
 const fleet = {id:config.fleetId,version:'1',budget_id:config.budgetId,storage_id:config.storageId,registry_id:config.registryId,chain_identifier:config.chainIdentifier,key_epoch:'0',support:[],health_interval_ms:'3600000',batch_interval_ms:'86400000',emergency_interval_ms:'300000',device_count:String(w.devices)};
 const object = (type, codec, value) => ({object:{owner:{$kind:'Shared'},type,content:codec.serialize(value).toBytes()}});
 const field = (type, bytes) => ({dynamicField:{value:{type,bcs:bytes}}});
 const storage = {id:config.storageId,version:'1',budget_id:config.budgetId,pool:null,wal:{value:'0'},default_quota:{period_ms:'86400000',encoded_bytes_per_period:'200000000',uploads_per_period:'3',max_blob_size:'4194304',write_fees_per_period:'3000000'},blob_count:String(w.devices),total_encoded_bytes:'0',total_write_fees:'0'};
 const client = {
  ledgerService: {async getServiceInfo() { if(rpcFault)throw transient('fixture RPC 429/outage'); return {response:{chainId:toBase58(Buffer.from(config.chainIdentifier+'00'.repeat(28),'hex'))}}; }},
  async getObject({objectId}) {
   if(rpcFault)throw transient('fixture RPC outage');
   if(objectId===config.fleetId)return object(`${config.typeOrigin}::fleet_data::FleetData`,data.FleetDataBcs,fleet);
   if(objectId===config.storageId)return object(`${storageOrigin}::fleet_storage::FleetStorage`,budget.FleetStorageBcs,storage);
   const e=byWriter.get(objectId); assert(e,'unknown fixture writer'); return object(`${config.typeOrigin}::fleet_data::DeviceData`,data.DeviceDataBcs,e.writer);
  },
  async getDynamicField({parentId,name}) {
   if(rpcFault)throw transient('fixture RPC outage');
   if(parentId===config.fleetId) {const e=byDevice.get(bcs.Address.parse(name.bcs));assert(e);return field('0x2::object::ID',bcs.Address.serialize(e.writer.id).toBytes());}
   if(parentId===config.registryId) {const e=byDevice.get(bcs.Address.parse(name.bcs));assert(e);return field(`${manifest.type_origins['device_registry::Identity']}::device_registry::Identity`,protocol.device_registry_IdentityBcs.serialize({signer:e.key.toSuiAddress(),revoked:false,owner_cap:id('0xc0')}).toBytes());}
   if(parentId===config.storageId) {const blob=bcs.Address.parse(name.bcs),e=entries.find(e=>e.anchor?.blob_object_id===blob);assert(e);return field(`${storageOrigin}::fleet_storage::BlobRecord`,bulk.BlobRecordBcs.serialize({device_id:e.device,blob_id:e.anchor.blob_id,size:String(e.ciphertext.length),encoded_size:'200000000',end_epoch:100,uploaded_at_ms:String(now)}).toBytes());}
   const e=byWriter.get(parentId);assert(e);if(delayed)throw transient('fixture delayed index');
   return field(`${config.typeOrigin}::fleet_data::Anchor`,data.AnchorBcs.serialize(e.anchor).toBytes());
  },
  async listDynamicFields({cursor,limit}) {assert.equal(limit,100);const start=cursor?Number(cursor):0, slice=entries.slice(start,start+limit),end=start+slice.length;
   return {dynamicFields:slice.map(e=>({$kind:'DynamicField',name:{type:`${config.typeOrigin}::fleet_data::DeviceKey`,bcs:bcs.Address.serialize(e.device).toBytes()},valueType:'0x2::object::ID'})),hasNextPage:end<entries.length,cursor:end<entries.length?String(end):null};}
 };
 const KS=bcs.struct('KeyServer',{id:bcs.Address,firstVersion:bcs.u64(),lastVersion:bcs.u64()});
 const KSV=bcs.struct('KeyServerV1',{name:bcs.string(),url:bcs.string(),keyType:bcs.u8(),pk:bcs.byteVector()});
 const seal = new SealClient({suiClient:{core:{async getObject({objectId}) {return {object:objectId===config.typeOrigin?{version:'1'}:{content:KS.serialize({id:objectId,firstVersion:1,lastVersion:1}).toBytes()}};},async getDynamicField({parentId}) {const server=vectors.servers.find(s=>s.object_id===parentId);assert(server);return field('',KSV.serialize({name:'fixture',url:'https://fixture.invalid',keyType:0,pk:Buffer.from(server.public_key,'hex')}).toBytes());}}},serverConfigs:vectors.servers.map(s=>({objectId:s.object_id,weight:1})),verifyKeyServers:false});
 for(let n=0;n<w.devices;n++) {
  const key=Ed25519Keypair.generate(),device=id('0x'+(0x1000+n).toString(16)),writerId=id('0x'+(0x2000+n).toString(16));
  const snapshot={...structuredClone(require('../interfaces/telemetry-cases.json').signed.snapshot),chain_identifier:config.chainIdentifier,registry:config.registryId,device_id:device,signer:key.toSuiAddress(),seq:1,observed_ms:now,started_ms:now-1000,sync:{last_success_ms:now,consecutive_failures:0,last_error:null}};
  const message=Buffer.from(JSON.stringify(snapshot)),signed=await key.signPersonalMessage(message);
  const e={key,device,writer:{id:writerId,version:'1',fleet_id:config.fleetId,device_id:device,account_id:id('0x'+(0x3000+n).toString(16)),health_seq:'1',health_at_ms:String(now),message:[...message],signature:[...Buffer.from(signed.signature,'base64')],batch_seq:'1',batch_at_ms:String(now),emergency_at_ms:null,anchor_count:'1'}};
  entries.push(e);byDevice.set(device,e);byWriter.set(writerId,e);
 }
 let cursor=null, discovered=[];
 do {const page=await measured('index_page',()=>data.listFleetDeviceIds(client,config,cursor));discovered.push(...page.devices);cursor=page.hasNextPage?page.cursor:null;}while(cursor);
 assert.equal(new Set(discovered).size,w.devices);
 for(let start=0;start<entries.length;start+=100) {
  const devices=entries.slice(start,start+100).map(e=>e.device);
  const health=await measured('health_page',()=>data.fetchChainHealth(client,config,devices,now));
  for(const d of devices)assert.equal(health[d].observedMs,now,JSON.stringify(health[d]));
 }
 const walrus={async systemState(){return {committee:{epoch:1}};}};
 const decrypt = e => async bytes => {
  keyRequests++;if(keyFault)throw transient('fixture keyserver outage');
  const parsed=EncryptedObject.parse(bytes),{blob,aad}=parsed.ciphertext.Aes256Gcm;
  const cipher=createDecipheriv('aes-256-gcm',e.demKey,Buffer.from([138,55,153,253,198,46,121,219,160,128,89,7,214,156,148,220]));
  cipher.setAAD(Buffer.from(aad));cipher.setAuthTag(Buffer.from(blob.slice(-16)));
  return Uint8Array.from(Buffer.concat([cipher.update(blob.slice(0,-16)),cipher.final()]));
 };
 const fetcher = e => async () => {if(gatewayFault)throw transient('fixture gateway 429/outage');return new Response(e.ciphertext);};
 const fetchVerified = (e,c=config) => bulk.fetchVerifiedArchive(client,walrus,c,e.device,0,'https://fixture.invalid',decrypt(e),fetcher(e));
 let totalCiphertextBytes=0;
 for(let round=0;round<w.rounds;round++) {
  const offset=stats.sign_encrypt?.length||0;
  peakConcurrency=Math.max(peakConcurrency,await pool(entries,w.concurrency,async e=>{
   await measured('sign_encrypt',async()=>{
    const count=Math.ceil(w.record_payload_bytes[round]/1024),line='x'.repeat(1024);
    const archive={schema:1,kind:'fleet-data',chain_identifier:config.chainIdentifier,registry:config.registryId,fleet_id:config.fleetId,device_id:e.device,signer:e.key.toSuiAddress(),seq:round+1,observed_ms:now,reason:'logs',dropped_lines:0,records:Array.from({length:count},()=>({source:'daemon',line}))};
    const message=Buffer.from(JSON.stringify(archive)),signed=await e.key.signPersonalMessage(message),plain=Buffer.from(JSON.stringify({message:message.toString('base64'),signature:signed.signature}));
    e.anchor={seq:String(round+1),kind:0,blob_object_id:id('0x'+(0x4000+entries.indexOf(e)).toString(16)),blob_id:String(1+entries.indexOf(e)),ciphertext_sha256:[],signer:e.key.toSuiAddress(),at_ms:String(now),end_epoch:100,key_epoch:'0'};
    e.writer.batch_seq=String(round+1);
    const identity=Buffer.concat([Buffer.from(config.fleetId.slice(2),'hex'),Buffer.alloc(8),randomBytes(32)]).toString('hex');
    const encrypted=await seal.encrypt({threshold:2,packageId:config.typeOrigin,id:identity,data:plain,aad:bulk.archiveAad(config,e.anchor),demType:DemType.AesGcm256});
    plain.fill(0);e.demKey=encrypted.key;e.ciphertext=Buffer.concat([Buffer.from('suifleet/seal/v1\n'),encrypted.encryptedObject]);
    e.anchor.ciphertext_sha256=[...Buffer.from(hash(e.ciphertext),'hex')];totalCiphertextBytes+=e.ciphertext.length;
   });
   const opened=await measured('verified_fetch',()=>fetchVerified(e));assert.equal(opened.device_id,e.device);
   // Retain one artifact for adversarial/recovery tests; release all other keys.
   if(e!==entries[0]) {e.demKey.fill(0);e.demKey=null;e.ciphertext=null;}
  }));
  byPayload.push({payload_bytes:w.record_payload_bytes[round],sign_encrypt:distribution(stats.sign_encrypt.slice(offset)),verified_fetch:distribution(stats.verified_fetch.slice(offset))});
 }
 const e=entries[0];
 for(const fault of ['rpc','gateway','keyserver','delayed-index']) {
  const toggle=on=>{rpcFault=fault==='rpc'&&on;gatewayFault=fault==='gateway'&&on;keyFault=fault==='keyserver'&&on;delayed=fault==='delayed-index'&&on;};
  toggle(true);let attempts=0;await assert.rejects(retry(()=>fetchVerified(e),w.retry,()=>attempts++));assert.equal(attempts,w.retry.attempts);
  toggle(false);assert.equal((await fetchVerified(e)).device_id,e.device);faults.push({fault,attempts,recovered:true});
 }
 rpcFault=true;const unavailable=await data.fetchChainHealth(client,config,[e.device],now);assert.equal(unavailable[e.device].status,'unavailable');rpcFault=false;
 const before=keyRequests;
 await assert.rejects(fetchVerified(e,{...config,fleetId:id('0x99')}));assert.equal(keyRequests,before,'foreign fleet rejected before keys');
 const saved=e.ciphertext;e.ciphertext=Buffer.from(saved);e.ciphertext[e.ciphertext.length-1]^=1;
 await assert.rejects(fetchVerified(e),/digest/);assert.equal(keyRequests,before);e.ciphertext=saved;
 let attempts=0;await assert.rejects(retry(async()=>{throw Error('budget exhausted: nonretryable fixture');},w.retry,()=>attempts++));assert.equal(attempts,1);
 const stormPeak=await pool(Array.from({length:w.devices}),w.concurrency,()=>fetchVerified(e));assert(stormPeak<=w.concurrency);
 const summaries=Object.fromEntries(Object.entries(stats).map(([k,v])=>[k,distribution(v)]));
 const rss=process.resourceUsage().maxRSS*1024, failures=[];
 for(const name of Object.keys(summaries))if(summaries[name].p95_ms>w.thresholds[`${name}_p95_ms`])failures.push(`${name} p95 threshold`);
 if(rss>w.thresholds.peak_rss_bytes)failures.push('RSS threshold');
 e.demKey.fill(0);
 return {schema:1,mode:'crypto-consumer-fixtures',result:failures.length?'FAIL':'PASS',failures,
  workload_sha256:hash(JSON.stringify(w)),revision,node:process.version,
  elapsed_ms:performance.now()-started,cpu_microseconds:process.cpuUsage(cpuStart),peak_rss_bytes:rss,devices:w.devices,rounds:w.rounds,
  peak_concurrency:peakConcurrency,reconnect_peak_concurrency:stormPeak,ciphertext_bytes:totalCiphertextBytes,measurements:summaries,by_payload:byPayload,raw_samples_ms:stats,faults,
  negative_checks:['RPC outage returns unavailable health','foreign fleet before key request','ciphertext digest before key request','nonretryable synthetic budget exhaustion'],
  spend:{sui_mist:'0',wal_frost:'0',scope:'no network I/O or chain transactions'},
  limits:['synthetic chain/provider I/O','test-only DEM keys, no real provider authorization','driver retry/concurrency, not product scheduler','not enrollment/deployment/receipt/budget/chain throughput','not real browser rendering or paid-pilot support','SF-082 merged-main integration and isolated/testnet load acceptance outstanding']};
}
if(require.main===module) {
 const [workloadFile,receiptFile]=process.argv.slice(2);if(!workloadFile||!receiptFile)throw Error('Usage: node scripts/pilot_load.cjs WORKLOAD.json RECEIPT.json');
 fs.rmSync(receiptFile,{force:true});
 run(JSON.parse(fs.readFileSync(workloadFile,'utf8'))).then(result=>{fs.writeFileSync(receiptFile,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({result:result.result,measurements:result.measurements,limits:result.limits}));if(result.result!=='PASS')process.exitCode=1;}).catch(error=>{console.error(error.stack);process.exitCode=1;});
}
module.exports={validate,distribution,pool,retry,run};
