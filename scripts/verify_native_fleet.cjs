const assert=require('node:assert/strict'),fs=require('node:fs');
const {loader,appRequire}=require('./frontend_test_utils.cjs');
const f=JSON.parse(fs.readFileSync(process.argv[2]));process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(f.manifest);
const {bcs}=appRequire('@mysten/sui/bcs'),{toBase58,normalizeSuiAddress:id}=appRequire('@mysten/sui/utils');
const load=loader(),data=load('lib/fleet-data.ts'),protocol=load('lib/fleet-protocol.ts'),bulk=load('lib/fleet-bulk.ts');
const writer=f.writer,budget=load('lib/fleet-budget.ts'),storageOrigin=id('0xf6');
const chain={ledgerService:{async getServiceInfo(){return {response:{chainId:toBase58(Buffer.concat([Buffer.from(f.manifest.chain_identifier,'hex'),Buffer.alloc(28)]))}};}},
 async getObject({objectId}){if(objectId===f.config.storageId)return {object:{owner:{$kind:'Shared'},type:`${storageOrigin}::fleet_storage::FleetStorage`,content:budget.FleetStorageBcs.serialize({id:f.config.storageId,version:'1',budget_id:f.config.budgetId,pool:null,wal:{value:'0'},default_quota:{period_ms:'1',encoded_bytes_per_period:'1',uploads_per_period:'1',max_blob_size:'1',write_fees_per_period:'1'},blob_count:String(f.anchors.length),total_encoded_bytes:'1',total_write_fees:'1'}).toBytes()}};return {object:{owner:{$kind:'Shared'},type:`${f.config.typeOrigin}::fleet_data::${objectId===f.config.fleetId?'FleetData':'DeviceData'}`,
 content:objectId===f.config.fleetId?data.FleetDataBcs.serialize(f.fleet).toBytes():data.DeviceDataBcs.serialize(writer).toBytes()}};},
 async getDynamicField({parentId,name}){if(parentId===f.config.storageId){const blob=id(bcs.Address.parse(name.bcs)),anchor=f.anchors.find(a=>id(a.blob_object_id)===blob);assert(anchor);return {dynamicField:{value:{type:`${storageOrigin}::fleet_storage::BlobRecord`,bcs:bulk.BlobRecordBcs.serialize({device_id:f.device,blob_id:anchor.blob_id,size:'1',encoded_size:'1',end_epoch:anchor.end_epoch,uploaded_at_ms:'1'}).toBytes()}}};}if(parentId===writer.id){const index=Number(bcs.u64().parse(name.bcs));return {dynamicField:{value:{type:`${f.config.typeOrigin}::fleet_data::Anchor`,bcs:data.AnchorBcs.serialize(f.anchors[index]).toBytes()}}};}return {dynamicField:{value:parentId===f.config.fleetId?{type:'0x2::object::ID',bcs:bcs.Address.serialize(writer.id).toBytes()}:
 {type:`${f.manifest.type_origins['device_registry::Identity']}::device_registry::Identity`,bcs:protocol.device_registry_IdentityBcs.serialize({signer:f.signer,owner_cap:id("0xc1"),revoked:false}).toBytes()}}};}};
(async()=>{
 const verified=(await data.fetchChainHealth(chain,f.config,[f.device],f.now))[f.device];
 const reported=JSON.parse(Buffer.from(writer.message).toString());
 assert(['healthy','degraded'].includes(reported.health));
 assert.equal(verified.status,reported.health,JSON.stringify(verified));
 if(f.anchors?.length){
   const path=require('node:path'),{pathToFileURL}=require('node:url'),vectors=require('../interfaces/seal-vectors.json');
   const root=path.dirname(appRequire.resolve('@mysten/seal'));
   const {EncryptedObject}=appRequire('@mysten/seal');
   const {decrypt}=await import(pathToFileURL(path.join(root,'decrypt.mjs')));
   const {hashToG1}=await import(pathToFileURL(path.join(root,'kdf.mjs')));
   const {Scalar,G2Element}=await import(pathToFileURL(path.join(root,'bls12381.mjs')));
   const {createFullId}=await import(pathToFileURL(path.join(root,'utils.mjs')));
   const open=async bytes=>{
     const encryptedObject=EncryptedObject.parse(bytes),fullId=createFullId(encryptedObject.packageId,encryptedObject.id);
     const keys=new Map(vectors.servers.map(s=>[`${fullId}:${s.object_id}`,hashToG1(Buffer.from(fullId.replace(/^0x/,''),'hex')).multiply(Scalar.fromBytes(Buffer.from(s.master_secret,'hex')))]));
     return decrypt({encryptedObject,keys,publicKeys:vectors.servers.map(s=>G2Element.fromBytes(Buffer.from(s.public_key,'hex'))),checkLEEncoding:false});
   };
   for(let index=0;index<f.anchors.length;index++){
     const anchor=f.anchors[index],ciphertext=Buffer.from(f.ciphertexts[anchor.blob_object_id],'base64');
     const fetcher=async()=>new Response(ciphertext);
     const archive=await bulk.fetchVerifiedArchive(chain,{systemState:async()=>({committee:{epoch:1}})},f.config,f.device,index,'https://fixture.invalid',open,fetcher);
     assert.equal(archive.reason,['logs','diagnostics','crash'][anchor.kind]);
     assert(!JSON.stringify(archive).includes('fixture-confidential-value'),'collector must redact before signing and encryption');
     const bad=Uint8Array.from(ciphertext);bad[bad.length-1]^=1;
     await assert.rejects(bulk.openArchive(bad,f.config,f.device,anchor,open),/digest/);
   }
   console.log('Native encrypted logs/diagnostics -> simulated prepaid anchors -> real SDK threshold decrypt -> browser signature/binding verification; redaction/tamper checked');
 }
 const original=writer.message.slice();writer.message[writer.message.length-2]^=1;
 assert.equal((await data.fetchChainHealth(chain,f.config,[f.device],f.now))[f.device].status,'unavailable');writer.message=original;
 writer.health_seq=String(BigInt(writer.health_seq)+1n);
 assert.equal((await data.fetchChainHealth(chain,f.config,[f.device],f.now))[f.device].status,'unavailable');
 console.log('Native daemon personal-message bytes independently verified by browser; tamper/replay refused');
})().catch(e=>{console.error(e);process.exit(1)});
