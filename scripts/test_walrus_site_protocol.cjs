const assert=require('node:assert/strict'),fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const P=require('./walrus_site_protocol.cjs');
(async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'sf082-site-protocol-'));
 try{
  const digest='ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad';
  await fs.writeFile(path.join(root,'index.html'),'abc');
  const files={'/index.html':{bytes:3,sha256:digest}},packed=await P.pack(root,files);
  assert.equal(packed.bytes.toString(),'abc');assert.equal(packed.resources[0].start,0);assert.equal(packed.resources[0].end,2);
  assert.equal(BigInt(packed.resources[0].hash),BigInt('0xad1500f261ff10b49c7a1796a36103b02322ae5dde404141eacf018fbf1678ba'));
  await fs.writeFile(path.join(root,'index.html'),'bad');await assert.rejects(P.pack(root,files),/changed resource/);
  await assert.rejects(P.pack(root,{'/../outside':{bytes:1,sha256:digest}}),/unsafe resource/);
  await fs.rm(path.join(root,'index.html'));await fs.symlink('/etc/passwd',path.join(root,'index.html'));await assert.rejects(P.pack(root,files));
  function client(snapshot){return {movePackageService:{getDatatype:async({moduleName,name})=>({response:{datatype:snapshot.types[moduleName+'::'+name]}})},getMoveFunction:async({moduleName,name})=>({function:snapshot.functions[moduleName+'::'+name]})};}
  await P.verifyAbi(client(structuredClone(P.pin)));
  for(const mutate of [x=>delete x.functions['site::new_resource'],x=>x.functions['site::new_resource'].parameters.pop(),x=>x.functions['site::add_resource'].parameters[0].reference='immutable',x=>x.types['site::Resource'].fields.reverse(),x=>x.types['site::Resource'].abilities=[]]){
   const changed=structuredClone(P.pin);mutate(changed);await assert.rejects(P.verifyAbi(client(changed)),'ABI drift must stop before signing');
  }
  assert.equal(P.reserve(100,60,20),80);assert.throws(()=>P.reserve(100,90,20));assert.throws(()=>P.reserve(100,0,0));
  assert.deepEqual(P.settle(80,20,{computationCost:'2',storageCost:'3',storageRebate:'50'}),{used:60,cost:0},'refund never replenishes the earlier spend');
  assert.deepEqual(P.settle(80,20,{computationCost:'7',storageCost:'5',storageRebate:'2'}),{used:70,cost:10});
  assert.throws(()=>P.settle(80,20,{}));assert.throws(()=>P.settle(80,20,{computationCost:'21',storageCost:'0',storageRebate:'0'}));
  console.log('Site pack ranges/SHA256 byte order, tampering/path/symlink refusal and complete pinned API/layout drift checks pass');
 }finally{await fs.rm(root,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
