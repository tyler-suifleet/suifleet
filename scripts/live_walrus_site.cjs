// gRPC adapter for the pinned Site contract. Only a precharged, disposable
// Testnet publisher key enters this process; the shared wallet never does.
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const {appRequire}=require('./frontend_test_utils.cjs'),P=require('./walrus_site_protocol.cjs');
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'),{Transaction}=appRequire('@mysten/sui/transactions'),{Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519'),{bcs}=appRequire('@mysten/sui/bcs'),{walrus,TESTNET_WALRUS_PACKAGE_CONFIG,blobIdToInt}=appRequire('@mysten/walrus'),{fromBase58,toHex,normalizeSuiAddress:id,normalizeStructTag}=appRequire('@mysten/sui/utils');
(async()=>{
 const request=JSON.parse(await fs.readFile(process.argv[2],'utf8')),journal=request.journal;
 const client=new SuiGrpcClient({network:'testnet',baseUrl:request.rpc});assert.equal(toHex(fromBase58((await client.getChainIdentifier()).chainIdentifier).slice(0,4)),'4c78adac');await P.verifyAbi(client);
 const storage=client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
 const system=await storage.systemObject();assert.equal(id(system.package_id),id(request.walrusPackage));
 const {pathToFileURL}=require('node:url');
 const {Blob}=await import(pathToFileURL(path.join(path.dirname(appRequire.resolve('@mysten/walrus')),'contracts/walrus/blob.mjs')));
 const blobOrigin=(await client.movePackageService.getDatatype({packageId:request.walrusPackage,moduleName:'blob',name:'Blob'})).response.datatype.definingId;
 async function freshBlob(objectId){const o=(await client.getObject({objectId,include:{content:true}})).object;assert.equal(normalizeStructTag(o.type),normalizeStructTag(`${blobOrigin}::blob::Blob`));const v=Blob.parse(o.content);assert.equal(v.id,id(objectId));return v;}
 const {writeBounded,requireWeightedQuorum}=await import('../dapp/scripts/walrus-node-writes.mjs');storage.writeEncodedBlobToNodes=opts=>writeBounded(storage,opts,opts.objectId);
 const certificate=storage.certificateFromConfirmations.bind(storage);storage.certificateFromConfirmations=async opts=>{const result=await certificate(opts);requireWeightedQuorum((await storage.systemState()).committee,result.signers);return result;};
 let state;try{state=JSON.parse(await fs.readFile(journal,'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;state={schema:1,owner:request.owner,used:request.initialUsed,limit:request.allowance,intents:{},blobs:{},phases:[]};}
 assert.equal(state.owner,request.owner);
 if(state.limit!==request.allowance){const e=request.expansion;assert(e && state.limit+e.amount===request.allowance && !state.expansions?.includes(e.digest));
  const result=await client.getTransaction({digest:e.digest,include:{effects:true,balanceChanges:true}});assert(result.Transaction?.status.success);
  const changes=result.Transaction.balanceChanges;assert(changes.some(c=>id(c.address)===request.owner && normalizeStructTag(c.coinType)===normalizeStructTag('0x2::sui::SUI') && BigInt(c.amount)===BigInt(e.amount)));assert(changes.some(c=>id(c.address)===id(e.operator) && normalizeStructTag(c.coinType)===normalizeStructTag('0x2::sui::SUI') && BigInt(c.amount)<=-BigInt(e.amount)));
  state.limit=request.allowance;(state.expansions??=[]).push(e.digest);
 }assert(state.used<=state.limit);
 async function save(){const f=await fs.open(journal+'.tmp','w',0o600);try{await f.writeFile(JSON.stringify(state,(_,v)=>typeof v==='bigint'?v.toString():v));await f.sync();}finally{await f.close();}await fs.rename(journal+'.tmp',journal);const d=await fs.open(path.dirname(journal),'r');try{await d.sync();}finally{await d.close();}}
 await save();
 const secret=(await fs.readFile(request.key,'utf8')).trim();assert(/^[0-9a-f]{64}$/.test(secret));const signer=Ed25519Keypair.fromSecretKey(Buffer.from(secret,'hex'));assert.equal(signer.toSuiAddress(),request.owner);
 const originalSign=signer.signTransaction.bind(signer);const allowed=new Set(['0x1','0x2',request.walrusPackage,P.pin.packageId].map(v=>id(v)));
 signer.signTransaction=async bytes=>{const d=Transaction.from(bytes).getData();assert.equal(id(d.sender),request.owner);assert.equal(id(d.gasData.owner??d.sender),request.owner);assert(BigInt(d.gasData.budget)>0n && BigInt(d.gasData.budget)<=500000000n);
  for(const c of d.commands){assert(['MoveCall','SplitCoins','MergeCoins','TransferObjects','MakeMoveVec'].includes(c.$kind),'unreviewed Site command');if(c.$kind==='MoveCall')assert(allowed.has(id(c.MoveCall.package)),'foreign Site call package');}return originalSign(bytes);};
 let busy=false;
 async function execute(label,transaction){assert(!busy,'parallel publisher transaction');busy=true;try{
  let intent=state.intents[label];if(!intent){assert(!Object.values(state.intents).some(i=>i.status==='unknown'),'unresolved earlier transaction');transaction.setSenderIfNotSet(request.owner);const bytes=await transaction.build({client}),d=Transaction.from(bytes).getData(),budget=Number(d.gasData.budget);P.reserve(state.limit,state.used,budget);
   intent=state.intents[label]={digest:await Transaction.from(bytes).getDigest(),bytes:Buffer.from(bytes).toString('base64'),reserved:budget,status:'unknown'};state.used=P.reserve(state.limit,state.used,budget);await save();}
  let result;try{result=await client.getTransaction({digest:intent.digest,include:{effects:true,events:true,objectTypes:true,balanceChanges:true}});}catch(e){if(e.name!=='TransactionError' && !String(e.message).includes('not found') && !String(e.message).includes('NOT_FOUND'))throw e;}
  if(!result){const bytes=Buffer.from(intent.bytes,'base64'),signed=await signer.signTransaction(bytes);result=await client.executeTransaction({transaction:bytes,signatures:[signed.signature],include:{effects:true,events:true,objectTypes:true,balanceChanges:true}});}
  const r=result.Transaction??result.FailedTransaction;assert(r && r.digest===intent.digest,'missing publisher execution effects');if(intent.status==='unknown'){const settled=P.settle(state.used,intent.reserved,r.effects.gasUsed);state.used=settled.used;const cost=settled.cost;intent.status=r.status.success?'success':'failure';intent.netGasMist=cost;await save();}
  assert(r.status.success,`Site transaction failed: ${label}`);await client.waitForTransaction({digest:r.digest});return result;
 }finally{busy=false;}}
 let blobContext='';signer.signAndExecuteTransaction=async({transaction})=>{
  const kind=await transaction.build({client,onlyTransactionKind:true});
  const names=Transaction.fromKind(kind).getData().commands.filter(c=>c.$kind==='MoveCall').map(c=>c.MoveCall.module+'::'+c.MoveCall.function).join(',');
  assert(names,'unresolved Walrus operation');return execute(`blob/${blobContext}/`+names,transaction);
 };
 const origin=name=>P.pin.types['site::'+name].definingId;
 async function verify(siteId,packed,routes){
  const site=(await client.getObject({objectId:siteId})).object;assert.equal(site.owner.AddressOwner,request.owner);
  const loaded={};for(const binding of Object.values(state.bindings))if(!loaded[binding.blobId])loaded[binding.blobId]=Buffer.from(await storage.readBlob({blobId:binding.blobId}));
  for(const expected of packed.resources){const binding=state.bindings[expected.path];assert(binding);const field=await client.getDynamicField({parentId:siteId,name:{type:`${origin('ResourcePath')}::site::ResourcePath`,bcs:bcs.struct('ResourcePath',{path:bcs.string()}).serialize({path:expected.path}).toBytes()}});
   assert.equal(normalizeStructTag(field.dynamicField.value.type),normalizeStructTag(`${origin('Resource')}::site::Resource`));const actual=P.Resource.parse(field.dynamicField.value.bcs);assert.equal(actual.path,expected.path);assert.equal(actual.blob_id,blobIdToInt(binding.blobId).toString());assert.equal(actual.blob_hash,expected.hash);assert.equal(actual.range.start,String(binding.start));assert.equal(actual.range.end,String(binding.end));assert.deepEqual(Object.fromEntries(actual.headers.contents.map(e=>[e.key,e.value])),binding.headers);assert.equal(P.sha(loaded[binding.blobId].subarray(binding.start,binding.end+1)),expected.sha256);}
  const field=await client.getDynamicField({parentId:siteId,name:{type:'vector<u8>',bcs:bcs.vector(bcs.u8()).serialize(Buffer.from('routes')).toBytes()}});const actual=Object.fromEntries(P.Routes.parse(field.dynamicField.value.bcs).route_list.contents.map(e=>[e.key,e.value]));assert.deepEqual(actual,routes);
  let cursor,count=0;do{const page=await client.listDynamicFields({parentId:siteId,cursor,limit:50});count+=page.dynamicFields.length;if(!page.hasNextPage)break;assert(page.cursor && page.cursor!==cursor);cursor=page.cursor;}while(true);assert.equal(count,packed.resources.length+1,'unexpected Site resource fields');
 }
 state.bindings??={};
 const operations=request.operations??[
  {name:'publish',marker:'SF082 update/rollback A\n'},
  {name:'update',marker:'SF082 update/rollback B\n'},
  {name:'rollback',marker:'SF082 update/rollback A\n'},
 ];
 assert(operations.length>0 && operations.length<=3 && new Set(operations.map(o=>o.name)).size===operations.length);
 let verifiedLast=false;
 for(const operation of operations){
  const phase=operation.name;assert(typeof phase==='string' && phase.length<160);
  if(state.phases.some(p=>p.phase===phase))continue;
  const directory=operation.directory??request.directory;
  const metadata=JSON.parse(await fs.readFile(path.join(directory,'ws-resources.json'),'utf8'));
  const files={...(operation.files??request.files)};
  if(operation.marker!==undefined){await fs.writeFile(path.join(directory,'acceptance.txt'),operation.marker);files['/acceptance.txt']={sha256:P.sha(Buffer.from(operation.marker)),bytes:Buffer.byteLength(operation.marker)};}
  const packed=await P.pack(directory,files),hash=P.sha(packed.bytes);blobContext=hash;
  let blob=state.blobs[hash]??={};
  if(blob.result){const current=await freshBlob(blob.result.blobObject.id);blob.result.blobObject=current;
   if(current && current.certified_epoch==null){const registered=Object.entries(state.intents).find(([label,i])=>label.startsWith(`blob/${hash}/`) && Transaction.from(i.bytes).getData().commands.some(c=>c.$kind==='MoveCall' && c.MoveCall.function==='register_blob'))?.[1];assert(registered?.status==='success');blob.step={step:'registered',blobId:blob.result.blobId,blobObjectId:blob.result.blobObject.id,txDigest:registered.digest};delete blob.result;await save();}}
  if(!blob.result){blob.result=await storage.writeBlob({blob:packed.bytes,epochs:3,deletable:false,signer,resume:blob.step,onStep:async step=>{blob.step=step;await save();}});await save();}
  blob.result.blobObject=await freshBlob(blob.result.blobObject.id);await save();
  P.reusableBlob(blob.result.blobObject,Number((await storage.systemState()).epoch));
  const planned=structuredClone(state.bindings);const {rebind:changed,removed}=P.resourcePlan(packed.resources,planned);
  const tx=new Transaction();let siteArg;
  if(!state.siteId){const [m]=tx.moveCall({target:`${P.pin.packageId}::metadata::new_metadata`,arguments:Array.from({length:5},()=>tx.pure.option('string',null))});[siteArg]=tx.moveCall({target:`${P.pin.packageId}::site::new_site`,arguments:[tx.pure.string('SuiFleet'),m]});}
  else{const o=(await client.getObject({objectId:state.siteId})).object;assert.equal(o.owner.AddressOwner,request.owner);siteArg=tx.object(state.siteId);}
  for(const name of removed){tx.moveCall({target:`${P.pin.packageId}::site::remove_resource_if_exists`,arguments:[siteArg,tx.pure.string(name)]});delete planned[name];}
  for(const resource of changed){if(state.siteId)tx.moveCall({target:`${P.pin.packageId}::site::remove_resource_if_exists`,arguments:[siteArg,tx.pure.string(resource.path)]});const [range]=tx.moveCall({target:`${P.pin.packageId}::site::new_range_option`,arguments:[tx.pure.option('u64',resource.start),tx.pure.option('u64',resource.end)]});const [r]=tx.moveCall({target:`${P.pin.packageId}::site::new_resource`,arguments:[tx.pure.string(resource.path),tx.pure.u256(blobIdToInt(blob.result.blobId)),tx.pure.u256(resource.hash),range]});
   const ext=path.extname(resource.path),mime={'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css','.json':'application/json','.txt':'text/plain; charset=utf-8','.svg':'image/svg+xml','.png':'image/png','.ico':'image/x-icon','.wasm':'application/wasm','.woff2':'font/woff2','.woff':'font/woff','.ttf':'font/ttf'}[ext]??'application/octet-stream';
   const headers={'Content-Type':mime,...(metadata.headers?.[resource.path]??{})};planned[resource.path]={blobId:blob.result.blobId,start:resource.start,end:resource.end,sha256:resource.sha256,headers};
   for(const [name,value] of Object.entries(headers))tx.moveCall({target:`${P.pin.packageId}::site::add_header`,arguments:[r,tx.pure.string(name),tx.pure.string(value)]});tx.moveCall({target:`${P.pin.packageId}::site::add_resource`,arguments:[siteArg,r]});}
  if(state.siteId && JSON.stringify(state.routes)!==JSON.stringify(metadata.routes)){tx.moveCall({target:`${P.pin.packageId}::site::remove_all_routes_if_exist`,arguments:[siteArg]});tx.moveCall({target:`${P.pin.packageId}::site::create_routes`,arguments:[siteArg]});tx.moveCall({target:`${P.pin.packageId}::site::fill_routes`,arguments:[siteArg,tx.pure.vector('string',Object.keys(metadata.routes)),tx.pure.vector('string',Object.values(metadata.routes))]});}
  if(!state.siteId){tx.moveCall({target:`${P.pin.packageId}::site::create_routes`,arguments:[siteArg]});tx.moveCall({target:`${P.pin.packageId}::site::fill_routes`,arguments:[siteArg,tx.pure.vector('string',Object.keys(metadata.routes)),tx.pure.vector('string',Object.values(metadata.routes))]});tx.transferObjects([siteArg],request.owner);}
  const result=await execute('site/'+phase,tx);if(!state.siteId){state.siteId=Object.keys(result.Transaction.objectTypes).find(k=>normalizeStructTag(result.Transaction.objectTypes[k])===normalizeStructTag(`${origin('Site')}::site::Site`));assert(state.siteId);await save();}
  state.bindings=planned;state.routes=metadata.routes;await save();await verify(state.siteId,packed,metadata.routes);if(operation===operations.at(-1))verifiedLast=true;if(!state.phases.some(p=>p.phase===phase))state.phases.push({phase,digest:result.Transaction.digest,siteId:state.siteId,blobId:blob.result.blobId,files,verifiedResources:packed.resources.length,verifiedRoutes:Object.keys(metadata.routes).length,fullWalrusReadback:true});await save();console.log(JSON.stringify({phase,siteId:state.siteId,resources:packed.resources.length}));
 }
 if(!verifiedLast){const last=operations.at(-1),directory=last.directory??request.directory,files={...(last.files??request.files)};
  if(last.marker!==undefined){await fs.writeFile(path.join(directory,'acceptance.txt'),last.marker);files['/acceptance.txt']={sha256:P.sha(Buffer.from(last.marker)),bytes:Buffer.byteLength(last.marker)};}
  const metadata=JSON.parse(await fs.readFile(path.join(directory,'ws-resources.json'),'utf8'));await verify(state.siteId,await P.pack(directory,files),metadata.routes);
 }
 console.log(JSON.stringify({result:'verified',siteId:state.siteId,usedMist:state.used,phases:state.phases.length}));
})().catch(async error=>{await fs.writeFile(process.argv[2]+'.error',String(error.stack),{mode:0o600}).catch(()=>{});console.error('Site gRPC publication/readback failed; private journal and charged reservations retained');process.exitCode=1});
