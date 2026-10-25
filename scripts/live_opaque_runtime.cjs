// Dedicated-testnet synthetic SF139 codec/reader acceptance. No keys enter Node.
const fs=require('node:fs'),path=require('node:path'),{createHash,randomBytes}=require('node:crypto');
const {appRequire,loader}=require('./frontend_test_utils.cjs');
const input=JSON.parse(fs.readFileSync(process.argv[2]));process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(input.manifest);
const load=loader(),opaque=load('lib/opaque-data.ts'),data=load('lib/fleet-data.ts'),bulk=load('lib/fleet-bulk.ts');
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'),{SealClient,SessionKey,NoAccessError}=appRequire('@mysten/seal'),
 {bcs}=appRequire('@mysten/sui/bcs'),{normalizeSuiAddress:id}=appRequire('@mysten/sui/utils'),{isValidPersonalMessageSignature}=appRequire('@mysten/sui/verify'),
 {walrus,TESTNET_WALRUS_PACKAGE_CONFIG}=appRequire('@mysten/walrus');
const lines=require('node:readline').createInterface({input:process.stdin})[Symbol.asyncIterator]();
const sha=b=>createHash('sha256').update(b).digest();
async function sign(message) {
 console.log(JSON.stringify({personal_message:Buffer.from(message).toString('base64')}));
 const response=await lines.next();if(response.done)throw Error('Signature bridge unavailable');
 const {signature}=JSON.parse(response.value);
 if(!await isValidPersonalMessageSignature(message,signature,{address:input.signer}))throw Error('Fixture signature mismatch');
 return signature;
}
(async()=>{
 if(input.config.chainIdentifier!=='4c78adac')throw Error('Testnet only');
 const client=new SuiGrpcClient({network:'testnet',baseUrl:input.manifest.rpc_url});
 const fleet=await data.readFleetData(client,input.config),writer=await data.readDeviceData(client,input.config,input.device);
 if(!writer)throw Error('Writer missing');
 const servers=input.servers.map(objectId=>({objectId:id(objectId),weight:1}));
 if(input.operation==='prepare') {
  const seq=Number(writer.batch_seq)+1,seal=new SealClient({suiClient:client,serverConfigs:servers,verifyKeyServers:true}),items=[],indexes=[];
  const payloads=[Buffer.from([0,255,128,0,10]),Buffer.from('public synthetic opaque text\n')];
  for(let n=0;n<payloads.length;n++) {
   const bytes=payloads[n],stream=n?'public-text':'public-binary';
   const descriptor={stream,key:'synthetic',content_type:n?'text/plain':'application/octet-stream',metadata:{},upload:sha(bytes).toString('hex'),chunk:0,chunks:1,total_size:bytes.length,size:bytes.length,sha256:[...sha(bytes)]};
   const message=Buffer.from(JSON.stringify({kind:'fleet-opaque',schema:1,chain_identifier:'4c78adac',registry:input.config.registryId,fleet_id:input.config.fleetId,
    device_id:id(input.device),signer:id(input.signer),batch_sequence:seq,item:{sequence:n,descriptor,data:bytes.toString('base64'),source_sequence:null}}));
   const plaintext=Buffer.from(JSON.stringify({message:message.toString('base64'),signature:await sign(message)}));
   const identity=Buffer.concat([Buffer.from(bcs.Address.serialize(input.config.fleetId).toBytes()),Buffer.from(bcs.u64().serialize(fleet.key_epoch).toBytes()),
    Buffer.from(bcs.Address.serialize(input.device).toBytes()),sha(Buffer.from(stream)),randomBytes(16)]);
   const aad=Buffer.from(`suifleet/opaque/v1/4c78adac/${input.config.fleetId}/${id(input.device)}/${seq}/${n}`);
   const {encryptedObject}=await seal.encrypt({threshold:2,packageId:input.config.typeOrigin,id:identity.toString('hex'),data:plaintext,aad});plaintext.fill(0);
   const ciphertext=Buffer.concat([Buffer.from('suifleet/seal/v1\n'),Buffer.from(encryptedObject)]),identifier=`data-0${n}`,file=path.join(input.directory,identifier+'.bin');
   fs.writeFileSync(file,ciphertext,{mode:0o600});items.push({identifier,path:file,size:ciphertext.length,sha256:sha(ciphertext).toString('hex')});
   const index={anchor_index:'0',sequence:String(n),...descriptor,metadata:Buffer.from('{}'),identifier,ciphertext_size:String(ciphertext.length),ciphertext_sha256:sha(ciphertext)};
   indexes.push(Buffer.from(opaque.DataItemBcs.serialize(index).toBytes()).subarray(8).toString('hex'));
  }
  const archive={schema:4,kind:'fleet-data',chain_identifier:'4c78adac',registry:input.config.registryId,fleet_id:input.config.fleetId,device_id:id(input.device),signer:id(input.signer),
   seq,observed_ms:Date.now(),reason:'logs',dropped_lines:0,records:[],items:items.map(({path,...v})=>v)};
  const message=Buffer.from(JSON.stringify(archive)),plaintext=Buffer.from(JSON.stringify({message:message.toString('base64'),signature:await sign(message)}));
  const identity=Buffer.concat([Buffer.from(bcs.Address.serialize(input.config.fleetId).toBytes()),Buffer.from(bcs.u64().serialize(fleet.key_epoch).toBytes()),randomBytes(16)]);
  const {encryptedObject}=await seal.encrypt({threshold:2,packageId:input.config.typeOrigin,id:identity.toString('hex'),data:plaintext,aad:bulk.archiveAad(input.config,{seq:String(seq),kind:0})});plaintext.fill(0);
  const ciphertext=Buffer.concat([Buffer.from('suifleet/seal/v1\n'),Buffer.from(encryptedObject)]);fs.writeFileSync(path.join(input.directory,'ciphertext.bin'),ciphertext,{mode:0o600});
  console.log(JSON.stringify({prepared:{seq,items,indexes,ciphertext_sha256:sha(ciphertext).toString('hex')}}));
 } else {
  const session=await SessionKey.create({address:input.signer,packageId:input.config.typeOrigin,ttlMin:5,suiClient:client});
  await session.setPersonalMessageSignature(await sign(session.getPersonalMessage(),input.signer));
  const storage=client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
  const results=[];
  for(let n=0;n<2;n++) {
   const index=await opaque.readDataItem(client,input.config,input.device,'1',n);
   const owner=await opaque.scopedDecryptor(client,input.config,{keyServers:servers,threshold:2},session,input.device,index.stream,{kind:'owner',ownerCapId:input.budgetCap});
   const bytes=await opaque.fetchDataItem(client,storage,input.config,input.device,'1',n,input.manifest.walrus.aggregator_url,owner);
   const expected=n?Buffer.from('public synthetic opaque text\n'):Buffer.from([0,255,128,0,10]);if(!Buffer.from(bytes).equals(expected))throw Error('Opaque bytes differ');
   const reader=await opaque.scopedDecryptor(client,input.config,{keyServers:servers,threshold:2},session,input.device,index.stream,{kind:'reader'});
   let denied=false;
   try{await opaque.fetchDataItem(client,storage,input.config,input.device,'1',n,input.manifest.walrus.aggregator_url,reader);}
   catch(e){if(!(e instanceof NoAccessError))throw e;denied=true;}
   if(denied!==(n===1||input.operation==='read-denied'))throw Error('Scoped reader authority differs');
   results.push({stream:index.stream,size:bytes.length,sha256:sha(bytes).toString('hex'),readerDenied:denied,index_bcs_bytes:opaque.DataItemBcs.serialize(index).toBytes().length});
  }
  console.log(JSON.stringify({result:{ownerVerified:true,items:results,scopedRevocation:input.operation==='read-denied'}}));
 }
})().then(()=>process.exit(0)).catch(e=>{console.error(e.stack);process.exit(1)});
