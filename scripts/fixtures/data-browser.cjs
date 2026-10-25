// Synthetic offline fixtures: real SDK encryption/signatures, public test DEM keys.
const {appRequire}=require('../frontend_test_utils.cjs');
const {Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519');
const {normalizeSuiAddress:id}=appRequire('@mysten/sui/utils');
const {bcs}=appRequire('@mysten/sui/bcs');
const {SealClient,DemType}=appRequire('@mysten/seal');
const {createHash}=require('node:crypto');
const vectors=require('../../interfaces/seal-vectors.json');
module.exports=async()=>{
  const config={schema:1,chainIdentifier:'4c78adac',registryId:id('0xa1'),packageId:id('0xa2'),typeOrigin:id('0xa2'),fleetId:id('0xf1'),budgetId:id('0xb1'),storageId:id('0xb2')};
  const key=Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(19));
  const KeyServer=bcs.struct('KeyServer',{id:bcs.Address,firstVersion:bcs.u64(),lastVersion:bcs.u64()});
  const KeyServerV1=bcs.struct('KeyServerV1',{name:bcs.string(),url:bcs.string(),keyType:bcs.u8(),pk:bcs.byteVector()});
  const chain={core:{getObject:async({objectId})=>({object:objectId===config.typeOrigin?{version:'1'}:{content:KeyServer.serialize({id:objectId,firstVersion:1,lastVersion:1}).toBytes()}}),
    getDynamicField:async({parentId})=>({dynamicField:{value:{bcs:KeyServerV1.serialize({name:'fixture',url:'https://fixture.invalid',keyType:0,pk:Buffer.from(vectors.servers.find(s=>s.object_id===parentId).public_key,'hex')}).toBytes()}}})}};
  const seal=new SealClient({suiClient:chain,serverConfigs:vectors.servers.map(s=>({objectId:s.object_id,weight:1})),verifyKeyServers:false});
  const sha=bytes=>Array.from(createHash('sha256').update(bytes).digest());
  const fixtures=[];
  for(const [device,stream,name,type,raw,upload] of [
    [id('0xd1'),'sensor','reading.json','application/json',Buffer.from('{"temperature":23,"label":"<script>unsafe()</script>"}'),'a'],
    [id('0xd2'),'camera','capture.bin','application/octet-stream',Buffer.from([0,255,128,1]),'b'],
    [id('0xd1'),'bulk','multipart.txt','text/plain',Buffer.alloc(24579,65),'c'],
  ]){
    for(let chunk=0;chunk<Math.ceil(raw.length/24576);chunk++){
      const bytes=raw.subarray(chunk*24576,(chunk+1)*24576),sequence=fixtures.length+1;
      const descriptor={stream,key:name,content_type:type,metadata:{site:'west'},upload:upload.repeat(64),chunk,chunks:Math.ceil(raw.length/24576),total_size:raw.length,size:bytes.length,sha256:sha(bytes)};
      const message=Buffer.from(JSON.stringify({kind:'fleet-opaque',schema:1,chain_identifier:config.chainIdentifier,registry:config.registryId,fleet_id:config.fleetId,device_id:device,signer:key.toSuiAddress(),batch_sequence:sequence,item:{sequence,descriptor,data:bytes.toString('base64')}}));
      const data=Buffer.from(JSON.stringify({message:message.toString('base64'),signature:(await key.signPersonalMessage(message)).signature}));
      const identity=Buffer.concat([Buffer.from(config.fleetId.slice(2),'hex'),Buffer.alloc(8),Buffer.from(device.slice(2),'hex'),Buffer.from(sha(Buffer.from(stream))),Buffer.alloc(16,sequence)]).toString('hex');
      const {encryptedObject,key:demKey}=await seal.encrypt({threshold:2,packageId:config.typeOrigin,id:identity,data,aad:Buffer.from(`suifleet/opaque/v1/${config.chainIdentifier}/${config.fleetId}/${device}/${sequence}/${sequence}`),demType:DemType.AesGcm256});
      const ciphertext=Buffer.concat([Buffer.from('suifleet/seal/v1\n'),encryptedObject]);
      const value={...descriptor,metadata:Array.from(Buffer.from(JSON.stringify(descriptor.metadata))),anchor_index:String(sequence-1),sequence:String(sequence),total_size:String(descriptor.total_size),size:String(bytes.length),identifier:'data-00',ciphertext_size:String(ciphertext.length),ciphertext_sha256:sha(ciphertext)};
      fixtures.push({row:{device,batch:String(sequence),item:0,atMs:String(Date.UTC(2026,9,23,12,sequence)),value},anchor:{seq:String(sequence),signer:key.toSuiAddress(),key_epoch:'0'},ciphertext:Array.from(ciphertext),demKey:Array.from(demKey)});
    }
  }
  return {config,fixtures,devices:[{id:id('0xd1'),name:'Sensor one'},{id:id('0xd2'),name:'Camera two'}],groups:[{objectId:id('0x91'),name:'West sensors',deviceIds:[id('0xd1')]}]};
};
