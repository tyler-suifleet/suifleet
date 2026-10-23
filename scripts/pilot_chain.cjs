// SF-090 disposable chain experiment. Publication setup follows SF-082 upgrade harness.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const {appRequire,loader}=require('./frontend_test_utils.cjs');
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'),{Transaction}=appRequire('@mysten/sui/transactions'),{Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519'),{requestSuiFromFaucetV2}=appRequire('@mysten/sui/faucet'),{bcs}=appRequire('@mysten/sui/bcs'),{fromBase58,toHex,normalizeSuiAddress:id}=appRequire('@mysten/sui/utils');
(async()=>{
 assert.equal(process.env.PROTOCOL_TEST_DISPOSABLE,'1');assert.deepEqual((await fs.readdir('/sys/class/net')).sort(),['lo']);
 let encrypted;
 const root=path.resolve(__dirname,'..'),tmp=await fs.mkdtemp(path.join(os.tmpdir(),'sf082-upgrade-'));
 try{
  const rpc=process.env.PROTOCOL_TEST_RPC,client=new SuiGrpcClient({network:'localnet',baseUrl:rpc});
  const chain=toHex(fromBase58((await client.getChainIdentifier()).chainIdentifier).slice(0,4));
  const owner=Ed25519Keypair.generate(),device=Ed25519Keypair.generate();
  for(const signer of [owner,device])await requestSuiFromFaucetV2({host:process.env.PROTOCOL_TEST_FAUCET,recipient:signer.toSuiAddress()});
  const wallet=path.join(tmp,'wallet');await fs.mkdir(wallet);await fs.writeFile(path.join(wallet,'sui.keystore'),'[]');
  await fs.writeFile(path.join(wallet,'client.yaml'),`keystore:\n  File: ${wallet}/sui.keystore\nenvs:\n- alias: localnet\n  rpc: ${rpc}\nactive_env: localnet\nactive_address: null\n`);
  const env={...process.env,SUI_CONFIG_DIR:wallet};
  const tree=path.join(tmp,'contracts');await fs.cp(path.join(root,'contracts'),tree,{recursive:true,filter:p=>!['build','Published.toml'].includes(path.basename(p))});
  const cache='/home/dev/.move/git',pin='https___github_com_MystenLabs_walrus_git_c68b530e0b1e522b4a3896190d883e4312a1cf0e';
  assert((await fs.readdir(cache)).includes(pin),'pinned Walrus source cache missing');
  const deps=path.join(tmp,'deps');await fs.mkdir(deps);
  for(const name of ['wal','walrus'])await fs.cp(path.join(cache,pin,'testnet-contracts',name),path.join(deps,name),{recursive:true,filter:p=>!['build','Published.toml'].includes(path.basename(p))});
  const keyDirectory=path.join(tmp,'seal-key-server');await fs.cp(path.join(root,'tests/seal-key-server'),keyDirectory,{recursive:true,filter:p=>!['build','Published.toml'].includes(path.basename(p))});
  const directories=[keyDirectory,tree,path.join(deps,'wal'),path.join(deps,'walrus'),path.join(tree,'fleet_storage'),path.join(tree,'fleet_data')];
  for(const directory of directories){
   const file=path.join(directory,'Move.toml');let text=(await fs.readFile(file,'utf8')).replaceAll('rev = "testnet-v1.82.1"','rev = "987123842623d6cb0e772dc665789cfd69bdec9f"');
   text=text.includes('[environments]')?text.replace('[environments]',`[environments]\nsf082 = "${chain}"`):text+`\n[environments]\nsf082 = "${chain}"\n`;
   for(const name of ['wal','walrus'])text=text.replace(new RegExp(`^${name} = \\{ git = [^\\n]+$`,'m'),`${name} = { local = "${deps}/${name}" }`);
   await fs.writeFile(file,text);
  }
  // Same isolated one-member Walrus initialization used by SF-092. This is
  // test setup only: the System/Storage/Blob mutation code remains upstream.
  await fs.appendFile(path.join(deps,'walrus/sources/system/system_state_inner.move'), `
public(package) fun pilot_fixture(ctx: &mut TxContext): SystemStateInnerV1 {
    let mut state = create_empty(104, ctx);
    let member = bls_aggregate::new_bls_committee_member(
        sui::bls12381::g1_to_uncompressed_g1(&sui::bls12381::g1_generator()),
        100, object::id_from_address(@0xabc));
    state.committee = bls_aggregate::new_bls_committee(0, vector[member]);
    state.total_capacity_size = 1_000_000_000;
    state.storage_price_per_unit_size = 5;
    state.write_price_per_unit_size = 1;
    state
}
`);
  await fs.appendFile(path.join(deps,'walrus/sources/system.move'), `
public fun pilot_fixture(ctx: &mut TxContext) {
    let mut system = System { id: object::new(ctx), version: VERSION,
        package_id: object::id_from_address(@0xabc), new_package_id: option::none() };
    dynamic_field::add(&mut system.id, VERSION, system_state_inner::pilot_fixture(ctx));
    transfer::share_object(system);
}
`);
  function compile(directory){
   let text;try{text=execFileSync('sui',['move','build','--path',directory,'--build-env','sf082','--no-tree-shaking','--dump-bytecode-as-base64'],{env,encoding:'utf8',maxBuffer:30*1024*1024,stdio:['ignore','pipe','pipe']});}
   catch(error){throw Error('isolated source compilation failed: '+String(error.stderr).slice(-1500));}
   return JSON.parse(text.slice(text.indexOf('{')));
  }
  const digests={}, transactions=[];
  async function execute(label,tx,signer=owner){const r=await client.signAndExecuteTransaction({transaction:tx,signer,include:{effects:true,objectTypes:true,events:true}});assert(r.Transaction?.status.success,`${label}: ${JSON.stringify(r.FailedTransaction?.status)}`);await client.waitForTransaction({digest:r.Transaction.digest});digests[label]=r.Transaction.digest;transactions.push({label,digest:r.Transaction.digest,gas:r.Transaction.effects.gasUsed});return r.Transaction;}
  const created=(r,suffix)=>Object.keys(r.objectTypes).find(k=>r.objectTypes[k].endsWith(suffix));
  async function publish(name,directory){const tx=new Transaction(),[cap]=tx.publish(compile(directory));tx.transferObjects([cap],owner.toSuiAddress());const r=await execute('publish '+name,tx),pkg=r.effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId,upgradeCap=created(r,'::package::UpgradeCap');
   await fs.writeFile(path.join(directory,'Published.toml'),`[published.sf082]\nchain-id = "${chain}"\npublished-at = "${pkg}"\noriginal-id = "${pkg}"\nversion = 1\ntoolchain-version = "1.81.1"\nbuild-config = { flavor = "sui", edition = "2024" }\nupgrade-capability = "${upgradeCap}"\n`);return {pkg,upgradeCap,r};}
  const core=await publish('core',tree),registry=created(core.r,'::device_registry::DeviceRegistry');
  const walPackage=await publish('WAL',path.join(deps,'wal')),walrusPackage=await publish('Walrus',path.join(deps,'walrus'));
  const systemTx=new Transaction();systemTx.moveCall({target:`${walrusPackage.pkg}::system::pilot_fixture`});const system=created(await execute('initialize synthetic Walrus system',systemTx),'::system::System');
  const storagePackage=await publish('storage',path.join(tree,'fleet_storage'));
  const dataPackage=await publish('data',path.join(tree,'fleet_data'));
  const workload=JSON.parse(await fs.readFile('docs/validation/sf090/isolated-workload.json'));
  assert.equal(workload.devices,16);assert.equal(workload.concurrency,4);
  const {performance}=require('node:perf_hooks'),measurements={},devices=[];
  const start=performance.now();
  async function measure(name,fn){const t=performance.now(),result=await fn();(measurements[name]??=[]).push(performance.now()-t);return result;}
  let tx=new Transaction();const [deposit]=tx.splitCoins(tx.gas,[tx.pure.u64(1000000000)]);
  const [limits]=tx.moveCall({target:`${core.pkg}::fleet_budget::new_limits`,arguments:[tx.pure.u64(50000000),tx.pure.u64(86400000),tx.pure.u64(10000000),tx.pure.u64(1),tx.pure.u64(0)]});
  const [bc]=tx.moveCall({target:`${core.pkg}::fleet_budget::create`,arguments:[tx.object(registry),deposit,limits,tx.pure.u64(0)]});tx.transferObjects([bc],owner.toSuiAddress());
  let r=await execute('create pilot budget',tx);const budget=created(r,'::fleet_budget::FleetBudget'),budgetCap=created(r,'::fleet_budget::BudgetOwnerCap');
  tx=new Transaction();const [quota]=tx.moveCall({target:`${storagePackage.pkg}::fleet_storage::new_quota`,arguments:[tx.pure.u64(86400000),tx.pure.u64(200000000),tx.pure.u64(1),tx.pure.u64(4194304),tx.pure.u64(2000000)]});
  tx.moveCall({target:`${storagePackage.pkg}::fleet_storage::create`,arguments:[tx.object(budget),tx.object(budgetCap),quota]});r=await execute('create pilot storage',tx);const storage=created(r,'::fleet_storage::FleetStorage');
  tx=new Transaction();tx.moveCall({target:`${dataPackage.pkg}::fleet_data::create`,arguments:[tx.object(budget),tx.object(budgetCap),tx.object(storage),tx.pure.string(chain)]});r=await execute('create pilot fleet',tx);const fleet=created(r,'::fleet_data::FleetData');
  for(let i=0;i<workload.devices;i++) {
   const key=Ed25519Keypair.generate();
   const d=await measure('enrollment',async()=>{
    let tx=new Transaction();const [device]=tx.moveCall({target:`${core.pkg}::device_registry::register_device`,arguments:[tx.object(registry),tx.pure.string(`pilot-${i}`),tx.pure.address(key.toSuiAddress()),tx.pure.string('synthetic'),tx.object('0x6')]});
    tx.moveCall({target:`${core.pkg}::device_cap::issue_cap`,arguments:[device]});
    const [oc]=tx.moveCall({target:`${core.pkg}::device_registry::activate_device`,arguments:[tx.object(registry),device]});
    tx.moveCall({target:`${core.pkg}::fleet_budget::enroll_device`,arguments:[tx.object(budget),tx.object(budgetCap),tx.object(registry),oc,tx.pure.u64(50000000),tx.pure.u64(10000000),tx.object('0x6')]});
    tx.transferObjects([oc],owner.toSuiAddress());const r=await execute(`enroll ${i}`,tx);
    return {key,deviceId:created(r,'::device_registry::Device'),cap:created(r,'::device_cap::DeviceCap'),account:created(r,'::fleet_budget::GasAccount')};
   });
   tx=new Transaction();tx.moveCall({target:`${dataPackage.pkg}::fleet_data::enroll`,arguments:[tx.object(fleet),tx.object(budget),tx.object(budgetCap),tx.object(d.account)]});r=await execute(`enroll writer ${i}`,tx);d.writer=created(r,'::fleet_data::DeviceData');devices.push(d);
  }
  const manifest=JSON.parse(await fs.readFile('manifests/fixture.localnet.json'));manifest.chain_identifier=chain;manifest.call_package_id=core.pkg;manifest.shared_objects.registry.id=registry;
  for(const name of Object.keys(manifest.type_origins))manifest.type_origins[name]=core.pkg;
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
  const load=loader(),data=load('lib/fleet-data.ts'),sui=load('lib/sui.ts');
  const config=data.parseDataConfig(JSON.stringify({schema:1,chainIdentifier:chain,registryId:registry,packageId:dataPackage.pkg,typeOrigin:dataPackage.pkg,fleetId:fleet,budgetId:budget,storageId:storage}));
  const {pool,distribution}=require('./pilot_load.cjs');
  encrypted=await require('./pilot_encrypted.cjs').start({client,rpc,core:core.pkg,registry,devices,owner,execute,created,publish,keyDirectory,tmp,workload,measure});
  const hashes={artifact:encrypted.artifactHash,recipe:encrypted.recipeHash};
  tx=new Transaction();tx.moveCall({target:`${core.pkg}::deployment_manager::create_deployment_verified`,arguments:[tx.pure.string('pilot'),tx.pure.string('1'),tx.pure.string(encrypted.blobId),tx.pure.string(hashes.artifact),tx.pure.string(hashes.recipe),tx.pure.vector('address',[]),tx.pure.vector('address',devices.map(d=>d.deviceId)),tx.object('0x6')]});
  r=await measure('deployment_admission',()=>execute('pilot deployment admission',tx));const deployment=created(r,'::deployment_manager::DeploymentRecord');
  // Independent per-device gas coins/accounts; the shared budget stays immutable.
  const peak=await pool(devices,workload.concurrency,async(d)=>{
   await measure('budget_draw',async()=>{const tx=new Transaction();tx.moveCall({target:`${core.pkg}::fleet_budget::draw_gas`,arguments:[tx.object(budget),tx.object(d.account),tx.object(registry),tx.object(d.cap),tx.pure.u64(10000000),tx.object('0x6')]});await execute(`draw ${d.deviceId}`,tx,d.key);});
   const now=Date.now(),snapshot={...structuredClone(JSON.parse(await fs.readFile('interfaces/telemetry-cases.json')).signed.snapshot),chain_identifier:chain,registry,device_id:d.deviceId,signer:d.key.toSuiAddress(),seq:1,observed_ms:now,started_ms:now-1000,sync:{last_success_ms:now,consecutive_failures:0,last_error:null}};
   const message=Buffer.from(JSON.stringify(snapshot)),signed=await d.key.signPersonalMessage(message);
   await measure('health_anchor',async()=>{const tx=new Transaction();tx.moveCall({target:`${dataPackage.pkg}::fleet_data::report_health`,arguments:[tx.object(fleet),tx.object(d.writer),tx.object(budget),tx.object(d.account),tx.object(registry),tx.object(d.cap),tx.pure.u64(1),tx.pure.vector('u8',message),tx.pure.vector('u8',Buffer.from(signed.signature,'base64')),tx.object('0x6')]});await execute(`health ${d.deviceId}`,tx,d.key);});
   await measure('telemetry_visibility',async()=>{const health=await data.fetchChainHealth(client,config,[d.deviceId]);assert.equal(health[d.deviceId].observedMs,now,JSON.stringify(health));});
   const expected=load('lib/move-bcs.ts').DeploymentBcs.parse((await client.getObject({objectId:deployment,include:{content:true}})).object.content);
   const recipeField=await client.getDynamicField({parentId:deployment,name:{type:`${core.pkg}::deployment_manager::RecipeDigestKey`,bcs:new Uint8Array([0])}});
   expected.recipe_digest=bcs.string().parse(recipeField.dynamicField.value.bcs);
   await encrypted.apply(d,expected);
   await encrypted.apply(d,expected); // repeat selection must not apply the file twice
   await measure('receipt_finalization',async()=>{const tx=new Transaction();tx.moveCall({target:`${core.pkg}::deployment_manager::report_verified_status`,arguments:[tx.object(deployment),tx.object(registry),tx.object(d.cap),tx.pure.u8(1)]});await execute(`receipt ${d.deviceId}`,tx,d.key);});
  });
  const receipts=await sui.readReceipts(client,deployment);assert.equal(receipts.verified,true);assert.equal(Object.keys(receipts.statuses).length,devices.length);for(const d of devices)assert.equal(receipts.statuses[d.deviceId],1);
  const page=await measure('index_page',()=>data.listFleetDeviceIds(client,config));assert.equal(page.devices.length,devices.length);assert.equal(page.hasNextPage,false);
  // A second draw exceeds the configured one-draw quota. Resolve every input
  // explicitly so the rejection is observed in chain effects, not simulation.
  const rejected=[];
  for(const d of devices.slice(0,4)) {
   const tx=new Transaction();tx.setSender(d.key.toSuiAddress());const coin=(await client.listCoins({owner:d.key.toSuiAddress()})).objects[0];tx.setGasPayment([{objectId:coin.objectId,version:coin.version,digest:coin.digest}]);tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice);tx.setGasBudget(5000000);
   async function shared(objectId,mutable){const o=(await client.getObject({objectId})).object;return tx.sharedObjectRef({objectId,initialSharedVersion:o.owner.Shared.initialSharedVersion,mutable});}
   const cap=(await client.getObject({objectId:d.cap})).object;
   tx.moveCall({target:`${core.pkg}::fleet_budget::draw_gas`,arguments:[await shared(budget,false),await shared(d.account,true),await shared(registry,false),tx.objectRef({objectId:d.cap,version:cap.version,digest:cap.digest}),tx.pure.u64(1),await shared('0x6',false)]});
   const bytes=await tx.build(),signed=await d.key.signTransaction(bytes),result=await client.executeTransaction({transaction:bytes,signatures:[signed.signature],include:{effects:true}});
   assert.equal(result.$kind,'FailedTransaction');assert.match(JSON.stringify(result.FailedTransaction.status),/fleet_budget::draw_gas/);await client.waitForTransaction({digest:result.FailedTransaction.digest});rejected.push({digest:result.FailedTransaction.digest,status:result.FailedTransaction.status,gas:result.FailedTransaction.effects.gasUsed});
  }
  // Race four authorized devices for a pool that fits only one small blob.
  // Registrations use real upstream storage accounting and execution effects.
  tx=new Transaction();const walCoin=created(walPackage.r,'::coin::Coin<'+walPackage.pkg+'::wal::WAL>');
  const [walDeposit]=tx.splitCoins(tx.object(walCoin),[tx.pure.u64(1000000)]);
  tx.moveCall({target:`${storagePackage.pkg}::fleet_storage::deposit_wal`,arguments:[tx.object(storage),tx.object(budget),tx.object(budgetCap),walDeposit]});
  const [nShards]=tx.moveCall({target:`${walrusPackage.pkg}::system::n_shards`,arguments:[tx.object(system)]});
  const [oneBlobBytes]=tx.moveCall({target:`${walrusPackage.pkg}::encoding::encoded_blob_length`,arguments:[tx.pure.u64(100),tx.pure.u8(1),nShards]});
  tx.moveCall({target:`${storagePackage.pkg}::fleet_storage::buy_storage`,arguments:[tx.object(storage),tx.object(budget),tx.object(budgetCap),tx.object(system),oneBlobBytes,tx.pure.u32(3)]});await execute('fund one-blob storage contention pool',tx);
  const storageResults=[];
  await pool(devices.slice(0,4),4,async(d)=>measure('storage_contention',async()=>{
   const tx=new Transaction();tx.setSender(d.key.toSuiAddress());const coin=(await client.listCoins({owner:d.key.toSuiAddress()})).objects[0];tx.setGasPayment([{objectId:coin.objectId,version:coin.version,digest:coin.digest}]);tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice);tx.setGasBudget(20000000);
   async function shared(objectId,mutable){const o=(await client.getObject({objectId})).object;return tx.sharedObjectRef({objectId,initialSharedVersion:o.owner.Shared.initialSharedVersion,mutable});}
   const cap=(await client.getObject({objectId:d.cap})).object;
   const [blob]=tx.moveCall({target:`${walrusPackage.pkg}::blob::derive_blob_id`,arguments:[tx.pure.u256(100+devices.indexOf(d)),tx.pure.u8(1),tx.pure.u64(100)]});
   tx.moveCall({target:`${storagePackage.pkg}::fleet_storage::register_blob`,arguments:[await shared(storage,true),await shared(budget,false),await shared(d.account,false),await shared(registry,false),tx.objectRef({objectId:d.cap,version:cap.version,digest:cap.digest}),await shared(system,true),blob,tx.pure.u256(100+devices.indexOf(d)),tx.pure.u64(100),tx.pure.u8(1),await shared('0x6',false)]});
   const bytes=await tx.build(),signed=await d.key.signTransaction(bytes),r=await client.executeTransaction({transaction:bytes,signatures:[signed.signature],include:{effects:true}}),out=r.Transaction||r.FailedTransaction;assert(out);
   await client.waitForTransaction({digest:out.digest});
   if(r.$kind==='FailedTransaction')assert.match(JSON.stringify(out.status),/fleet_storage::take_storage/);
   storageResults.push({device:d.deviceId,digest:out.digest,status:r.$kind==='Transaction'?'success':'insufficient_storage',gas:out.effects.gasUsed});
  }));
  assert.equal(storageResults.filter(r=>r.status==='success').length,1);
  const stored=load('lib/fleet-budget.ts').FleetStorageBcs.parse((await client.getObject({objectId:storage,include:{content:true}})).object.content);assert.equal(stored.blob_count,'1','failed purchases are atomic and must not fabricate records');
  const encryptedPipeline=await encrypted.verify();
  const summary=Object.fromEntries(Object.entries(measurements).map(([k,v])=>[k,distribution(v)]));
  const failures=Object.entries(summary).filter(([,v])=>v.p95_ms>workload.thresholds.stage_p95_ms).map(([k])=>`${k} p95 threshold`);
  const rss=process.resourceUsage().maxRSS*1024;if(rss>workload.thresholds.peak_rss_bytes)failures.push('RSS threshold');
  const net=gas=>BigInt(gas.computationCost)+BigInt(gas.storageCost)-BigInt(gas.storageRebate);
  const result={schema:1,mode:'isolated-real-chain',result:failures.length?'FAIL':'PASS',failures,revision:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),chain,devices:devices.length,peak_concurrency:peak,elapsed_ms:performance.now()-start,peak_rss_bytes:rss,measurements:summary,raw_samples_ms:measurements,encrypted_pipeline:encryptedPipeline,transactions,rejected,storage_contention:storageResults,
   net_gas_mist:[...transactions,...rejected,...storageResults].reduce((sum,t)=>sum+net(t.gas),0n).toString(),public_network_spend_mist:'0',
   limits:['local synthetic faucet gas; not public pricing','deployment receipt follows verified encrypted synthetic file application, not native daemon application','instrumented one-member isolated Walrus system; registrations only, not certified storage/provider capacity','no physical or production capacity claim']};
  await fs.writeFile(process.env.SF090_CHAIN_RECEIPT||'/tmp/sf090-chain.json',JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({result:result.result,measurements:summary,limits:result.limits}));if(failures.length)process.exitCode=1;
 }finally{if(encrypted)await encrypted.close();await fs.rm(tmp,{recursive:true,force:true});}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
