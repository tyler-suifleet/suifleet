const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{pathToFileURL}=require('node:url');
const {loader,appRequire}=require('./frontend_test_utils.cjs');
const f=JSON.parse(fs.readFileSync(process.argv[2]));process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(f.manifest);
const opaque=loader()('lib/opaque-data.ts'),vectors=require('../interfaces/seal-vectors.json');
(async()=>{
 const root=path.dirname(appRequire.resolve('@mysten/seal')),{EncryptedObject}=appRequire('@mysten/seal');
 const {decrypt}=await import(pathToFileURL(path.join(root,'decrypt.mjs'))),{hashToG1}=await import(pathToFileURL(path.join(root,'kdf.mjs'))),
 {Scalar,G2Element}=await import(pathToFileURL(path.join(root,'bls12381.mjs'))),{createFullId}=await import(pathToFileURL(path.join(root,'utils.mjs')));
 let requests=0;
 const open=async bytes=>{
   requests++;
   const encryptedObject=EncryptedObject.parse(bytes),fullId=createFullId(encryptedObject.packageId,encryptedObject.id);
   const keys=new Map(vectors.servers.map(s=>[`${fullId}:${s.object_id}`,hashToG1(Buffer.from(fullId.replace(/^0x/,''),'hex')).multiply(Scalar.fromBytes(Buffer.from(s.master_secret,'hex')))]));
   return decrypt({encryptedObject,keys,publicKeys:vectors.servers.map(s=>G2Element.fromBytes(Buffer.from(s.public_key,'hex'))),checkLEEncoding:false});
 };
 const parts={};
 for(const [key,value] of Object.entries(f.index)) {
   const [seq,n]=key.split('/'),index=opaque.DataItemBcs.parse(Buffer.from(value,'hex')),anchor=f.anchors.find(a=>a.seq===seq);
   const patch=f.patches[anchor.blob_object_id].find(p=>p.identifier===index.identifier),bytes=Buffer.from(patch.ciphertext,'base64');
   const result=await opaque.openDataItem(bytes,f.config,f.device,anchor,index,open);
   (parts[index.stream]??=[]).push({index,bytes:result});
   const before=requests,bad=Buffer.from(bytes);bad[bad.length-1]^=1;
   await assert.rejects(opaque.openDataItem(bad,f.config,f.device,anchor,index,open),/digest/);
   await assert.rejects(opaque.openDataItem(bytes,f.config,f.device,anchor,{...index,stream:'another'},open),/scope/);
   await assert.rejects(opaque.openDataItem(bytes,f.config,'0x123',anchor,index,open),/scope/);
   await assert.rejects(opaque.openDataItem(bytes,f.config,f.device,{...anchor,key_epoch:'1'},index,open),/scope/);
   assert.equal(requests,before,'tamper/scope refusal before requesting keys');
   await assert.rejects(opaque.openDataItem(bytes,f.config,f.device,anchor,{...index,key:'forged'},open),/metadata/);
 }
 for(const [stream,items] of Object.entries(parts)) {
   assert.deepEqual(Buffer.from(opaque.assembleDataItems(items)),Buffer.from(f.expected[stream],'base64'));
   assert.throws(()=>opaque.assembleDataItems(items.map(p=>({...p,bytes:Uint8Array.from(p.bytes)}))),/Unverified|provenance/);
   assert.throws(()=>opaque.assembleDataItems(items.map((p,n)=>n? p:{...p,index:{...p.index,key:'forged'}})),/provenance/);
   if(items[0].bytes.length){items[0].bytes[0]^=1;assert.throws(()=>opaque.assembleDataItems(items),/provenance/);items[0].bytes[0]^=1;}
   if(items.length>1)assert.throws(()=>opaque.assembleDataItems(items.slice(1)),/Incomplete/);
 }
 assert.deepEqual(Object.keys(parts).sort(),Object.keys(f.expected).sort());
 console.log('PASS native signatures/Seal ciphertext independently decrypted with pinned SDK; raw binary/text, chunk assembly, signed metadata, pre-key ciphertext/scope/epoch tamper refusal');
})().catch(e=>{console.error(e);process.exitCode=1});
