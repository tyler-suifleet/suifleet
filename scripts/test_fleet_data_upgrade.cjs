// Real package publication/upgrades on a disposable, network-none Sui chain.
// Walrus dependencies are their pinned sources, relocated to this chain; no stubs.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const {appRequire,loader}=require('./frontend_test_utils.cjs');
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'),{Transaction}=appRequire('@mysten/sui/transactions'),{Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519'),{requestSuiFromFaucetV2}=appRequire('@mysten/sui/faucet'),{bcs}=appRequire('@mysten/sui/bcs'),{fromBase58,toHex,normalizeSuiAddress:id}=appRequire('@mysten/sui/utils');
(async()=>{
 assert.equal(process.env.PROTOCOL_TEST_DISPOSABLE,'1');assert.deepEqual((await fs.readdir('/sys/class/net')).sort(),['lo']);
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
  const directories=[tree,path.join(deps,'wal'),path.join(deps,'walrus'),path.join(tree,'fleet_storage'),path.join(tree,'fleet_data')];
  for(const directory of directories){
   const file=path.join(directory,'Move.toml');let text=(await fs.readFile(file,'utf8')).replaceAll('rev = "testnet-v1.82.1"','rev = "987123842623d6cb0e772dc665789cfd69bdec9f"');
   text=text.includes('[environments]')?text.replace('[environments]',`[environments]\nsf082 = "${chain}"`):text+`\n[environments]\nsf082 = "${chain}"\n`;
   for(const name of ['wal','walrus'])text=text.replace(new RegExp(`^${name} = \\{ git = [^\\n]+$`,'m'),`${name} = { local = "${deps}/${name}" }`);
   await fs.writeFile(file,text);
  }
  function compile(directory){
   let text;try{text=execFileSync('sui',['move','build','--path',directory,'--build-env','sf082','--no-tree-shaking','--dump-bytecode-as-base64'],{env,encoding:'utf8',maxBuffer:30*1024*1024,stdio:['ignore','pipe','pipe']});}
   catch(error){throw Error('isolated source compilation failed: status='+error.status+' signal='+error.signal+' '+String(error.stderr).slice(-1500)+' '+String(error.stdout).slice(-500));}
   return JSON.parse(text.slice(text.indexOf('{')));
  }
  const digests={};
  async function execute(label,tx,signer=owner){const r=await client.signAndExecuteTransaction({transaction:tx,signer,include:{effects:true,objectTypes:true,events:true}});assert(r.Transaction?.status.success,`${label}: ${JSON.stringify(r.FailedTransaction?.status)}`);await client.waitForTransaction({digest:r.Transaction.digest});digests[label]=r.Transaction.digest;return r.Transaction;}
  const created=(r,suffix)=>Object.keys(r.objectTypes).find(k=>r.objectTypes[k].endsWith(suffix));
  async function publish(name,directory){const tx=new Transaction(),[cap]=tx.publish(compile(directory));tx.transferObjects([cap],owner.toSuiAddress());const r=await execute('publish '+name,tx),pkg=r.effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId,upgradeCap=created(r,'::package::UpgradeCap');
   await fs.writeFile(path.join(directory,'Published.toml'),`[published.sf082]\nchain-id = "${chain}"\npublished-at = "${pkg}"\noriginal-id = "${pkg}"\nversion = 1\ntoolchain-version = "1.81.1"\nbuild-config = { flavor = "sui", edition = "2024" }\nupgrade-capability = "${upgradeCap}"\n`);return {pkg,upgradeCap,r};}
  const core=await publish('core',tree),registry=created(core.r,'::device_registry::DeviceRegistry');
  await publish('WAL',path.join(deps,'wal'));await publish('Walrus',path.join(deps,'walrus'));
  const storagePackage=await publish('storage',path.join(tree,'fleet_storage'));
  const dataTree=path.join(tree,'fleet_data'),headSource=await fs.readFile(path.join(dataTree,'sources/fleet_data.move'));
  const baseRef='18f337c8ed0611cad957717ebc35e767f103a5cf';await fs.writeFile(path.join(dataTree,'sources/fleet_data.move'),execFileSync('git',['show',`${baseRef}:contracts/fleet_data/sources/fleet_data.move`],{cwd:root}));
  const base=await publish('base data',dataTree);
  let tx=new Transaction();let [d]=tx.moveCall({target:`${core.pkg}::device_registry::register_device`,arguments:[tx.object(registry),tx.pure.string('upgrade-device'),tx.pure.address(device.toSuiAddress()),tx.pure.string('x86_64'),tx.object('0x6')]});
  tx.moveCall({target:`${core.pkg}::device_cap::issue_cap`,arguments:[d]});const [oc]=tx.moveCall({target:`${core.pkg}::device_registry::activate_device`,arguments:[tx.object(registry),d]});tx.transferObjects([oc],owner.toSuiAddress());
  let r=await execute('register existing device',tx);const deviceId=created(r,'::device_registry::Device'),deviceCap=created(r,'::device_cap::DeviceCap'),ownerCap=created(r,'::device_registry::DeviceOwnerCap');
  tx=new Transaction();const [deposit]=tx.splitCoins(tx.gas,[tx.pure.u64(100_000_000)]);const [limits]=tx.moveCall({target:`${core.pkg}::fleet_budget::new_limits`,arguments:[tx.pure.u64(10_000_000),tx.pure.u64(86400000),tx.pure.u64(10000000),tx.pure.u64(2),tx.pure.u64(0)]});const [cap]=tx.moveCall({target:`${core.pkg}::fleet_budget::create`,arguments:[tx.object(registry),deposit,limits,tx.pure.u64(0)]});tx.transferObjects([cap],owner.toSuiAddress());r=await execute('create budget',tx);const budget=created(r,'::fleet_budget::FleetBudget'),budgetCap=created(r,'::fleet_budget::BudgetOwnerCap');
  tx=new Transaction();tx.moveCall({target:`${core.pkg}::fleet_budget::enroll_device`,arguments:[tx.object(budget),tx.object(budgetCap),tx.object(registry),tx.object(ownerCap),tx.pure.u64(0),tx.pure.u64(0),tx.object('0x6')]});r=await execute('enroll account',tx);const account=created(r,'::fleet_budget::GasAccount');
  tx=new Transaction();const [quota]=tx.moveCall({target:`${storagePackage.pkg}::fleet_storage::new_quota`,arguments:[tx.pure.u64(86400000),tx.pure.u64(200000000),tx.pure.u64(2),tx.pure.u64(4194304),tx.pure.u64(2000000)]});tx.moveCall({target:`${storagePackage.pkg}::fleet_storage::create`,arguments:[tx.object(budget),tx.object(budgetCap),quota]});r=await execute('create actual storage',tx);const storage=created(r,'::fleet_storage::FleetStorage');
  tx=new Transaction();tx.moveCall({target:`${base.pkg}::fleet_data::create`,arguments:[tx.object(budget),tx.object(budgetCap),tx.object(storage),tx.pure.string(chain)]});r=await execute('create base fleet',tx);const fleet=created(r,'::fleet_data::FleetData');
  tx=new Transaction();tx.moveCall({target:`${base.pkg}::fleet_data::enroll`,arguments:[tx.object(fleet),tx.object(budget),tx.object(budgetCap),tx.object(account)]});r=await execute('create base writer',tx);const writer=created(r,'::fleet_data::DeviceData');
  const before=await Promise.all([fleet,writer].map(async objectId=>(await client.getObject({objectId,include:{content:true}})).object.content));
  await fs.writeFile(path.join(dataTree,'sources/fleet_data.move'),headSource);await fs.rm(path.join(dataTree,'Published.toml'));const built=compile(dataTree);
  tx=new Transaction();const [ticket]=tx.moveCall({target:'0x2::package::authorize_upgrade',arguments:[tx.object(base.upgradeCap),tx.pure.u8(0),tx.pure.vector('u8',built.digest)]});const receipt=tx.upgrade({modules:built.modules,dependencies:built.dependencies,package:base.pkg,ticket});tx.moveCall({target:'0x2::package::commit_upgrade',arguments:[tx.object(base.upgradeCap),receipt]});r=await execute('upgrade data',tx);const head=r.effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId;
  const pkg=bcs.Object.parse((await client.getObject({objectId:head,include:{objectBcs:true}})).object.objectBcs).data.Package;
  const origins=Object.fromEntries(pkg.typeOriginTable.map(o=>[o.datatypeName,o.package]));assert.equal(origins.FleetData,base.pkg);assert.equal(origins.DeviceData,base.pkg);assert.equal(origins.AnchorKey,base.pkg);assert.equal(origins.DiagnosticKey,head);
  for(let i=0;i<2;i++)assert.deepEqual((await client.getObject({objectId:[fleet,writer][i],include:{content:true}})).object.content,before[i]);
  tx=new Transaction();tx.moveCall({target:`${head}::fleet_data::request_diagnostics`,arguments:[tx.object(fleet),tx.object(writer),tx.object(budget),tx.object(budgetCap),tx.object('0x6')]});await execute('diagnostics on existing writer',tx);
  const field=await client.getDynamicField({parentId:writer,name:{type:`${head}::fleet_data::DiagnosticKey`,bcs:bcs.bool().serialize(false).toBytes()}});assert.equal(bcs.struct('DiagnosticRequest',{seq:bcs.u64(),at_ms:bcs.u64()}).parse(field.dynamicField.value.bcs).seq,'1');
  // SF139: use existing objects and both callable package versions. A fleet-wide
  // support grant must never authorize the new scoped identities.
  assert.equal(origins.ItemKey,head);assert.equal(origins.DataItem,head);assert.equal(origins.ReadGrantKey,head);
  const stream=Buffer.from(require('node:crypto').createHash('sha256').update('sensor').digest());
  const scoped=Buffer.concat([Buffer.from(bcs.Address.serialize(fleet).toBytes()),Buffer.alloc(8),Buffer.from(bcs.Address.serialize(deviceId).toBytes()),stream,Buffer.alloc(16)]);
  tx=new Transaction();tx.moveCall({target:`${head}::fleet_data::set_support`,arguments:[tx.object(fleet),tx.object(budget),tx.object(budgetCap),tx.pure.address(owner.toSuiAddress()),tx.pure.bool(true)]});await execute('legacy fleet support grant',tx);
  const scopedChecks=[];
  async function approve(pkg,fn,dev=deviceId,hash=stream,success=false) {
   const probe=new Transaction();probe.setSender(owner.toSuiAddress());
   const args=[probe.pure.vector('u8',scoped),probe.object(fleet)];
   if(fn==='seal_approve_owner'||fn==='seal_approve_scoped_owner')args.push(probe.object(budget),probe.object(budgetCap));
   if(fn.startsWith('seal_approve_scoped'))args.push(probe.pure.id(dev),probe.pure.vector('u8',hash));
   probe.moveCall({target:`${pkg}::fleet_data::${fn}`,arguments:args});
   const result=await client.simulateTransaction({transaction:probe,checksEnabled:false,include:{effects:true}});
   assert.equal(Boolean(result.Transaction?.status.success),success,JSON.stringify(result));
   scopedChecks.push({pkg,fn,dev,stream:Buffer.from(hash).toString('hex'),success});
  }
  for(const pkg of [base.pkg,head])for(const fn of ['seal_approve_owner','seal_approve_support'])await approve(pkg,fn);
  await approve(head,'seal_approve_scoped_owner',deviceId,stream,true);
  await approve(head,'seal_approve_scoped_reader');
  tx=new Transaction();tx.moveCall({target:`${head}::fleet_data::set_read_grant`,arguments:[tx.object(fleet),tx.object(budget),tx.object(budgetCap),tx.pure.id(deviceId),tx.pure.vector('u8',stream),tx.pure.address(owner.toSuiAddress()),tx.pure.bool(true)]});await execute('scoped stream grant on existing fleet',tx);
  await approve(head,'seal_approve_scoped_reader',deviceId,stream,true);
  await approve(head,'seal_approve_scoped_reader',deviceId,Buffer.alloc(32));
  await approve(head,'seal_approve_scoped_reader','0xdead',stream);
  tx=new Transaction();tx.moveCall({target:`${head}::fleet_data::set_read_grant`,arguments:[tx.object(fleet),tx.object(budget),tx.object(budgetCap),tx.pure.id(deviceId),tx.pure.vector('u8',stream),tx.pure.address(owner.toSuiAddress()),tx.pure.bool(false)]});await execute('revoke scoped stream grant',tx);
  await approve(head,'seal_approve_scoped_reader');
  console.log('PASS old/new fleet-wide approvals refuse scoped keys; owner/stream grant/revocation and cross-device/cross-stream isolated upgrade probes');
  // SF-137: signed services survive the serverless chain path, including an
  // existing writer after upgrade. No indexer/HTTP ingress process is started.
  const coreMetadata=bcs.Object.parse((await client.getObject({objectId:core.pkg,include:{objectBcs:true}})).object.objectBcs).data.Package;
  const registryObject=(await client.getObject({objectId:registry})).object;
  const manifest={...require('../manifests/fixture.localnet.json'),chain_identifier:chain,rpc_url:rpc,call_package_id:core.pkg,
   upgrade_cap_id:core.upgradeCap,package_version:coreMetadata.version,
   type_origins:Object.fromEntries(coreMetadata.typeOriginTable.map(o=>[o.moduleName+'::'+o.datatypeName,o.package])),
   shared_objects:{registry:{id:registry,initial_shared_version:registryObject.owner.Shared.initialSharedVersion}}};
  const config={schema:1,chainIdentifier:chain,registryId:registry,packageId:head,typeOrigin:base.pkg,fleetId:fleet,budgetId:budget,storageId:storage};
  const snapshot=structuredClone(require('../interfaces/telemetry-cases.json').signed_services_v2.snapshot);
  Object.assign(snapshot,{chain_identifier:chain,registry,device_id:deviceId,signer:device.toSuiAddress(),seq:1,observed_ms:Date.now(),started_ms:Date.now()-60000});
  const message=Buffer.from(JSON.stringify(snapshot)),signature=Buffer.from((await device.signPersonalMessage(message)).signature,'base64');
  tx=new Transaction();tx.moveCall({target:head+'::fleet_data::report_health',arguments:[tx.object(fleet),tx.object(writer),tx.object(budget),tx.object(account),tx.object(registry),tx.object(deviceCap),tx.pure.u64(1),tx.pure.vector('u8',message),tx.pure.vector('u8',signature),tx.object('0x6')]});
  await execute('serverless signed services health',tx,device);
  const manifestPath=path.join(tmp,'serverless-manifest.json');await fs.writeFile(manifestPath,JSON.stringify(manifest));
  const healthEnv={...process.env,NEXT_PUBLIC_FLEET_DATA:JSON.stringify(config),NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL:'http://127.0.0.1:1'};
  const health=JSON.parse(execFileSync(process.execPath,[path.join(root,'scripts/suifleet_ops.cjs'),manifestPath,'health',owner.toSuiAddress()],{env:healthEnv,encoding:'utf8'}));
  assert(['healthy','degraded'].includes(health[deviceId].status),JSON.stringify(health));
  assert.deepEqual(health[deviceId].services,snapshot.services);
  const alerts=JSON.parse(execFileSync(process.execPath,[path.join(root,'scripts/suifleet_ops.cjs'),manifestPath,'alerts',owner.toSuiAddress()],{env:healthEnv,encoding:'utf8'}));
  assert.equal(alerts.status,'ok');
  console.log('PASS: direct-chain CLI health/alerts with signed messaging/bridge/streams/shadows and deliberately unusable legacy URL, no application HTTP services');
  tx=new Transaction();tx.moveCall({target:`${core.pkg}::device_registry::revoke_device`,arguments:[tx.object(registry),tx.object(ownerCap)]});await execute('revoke device',tx);
  // Submit fully resolved transactions, so rejection is a real failed effect.
  const attacks={};
  async function shared(t,objectId,mutable){const o=(await client.getObject({objectId})).object;return t.sharedObjectRef({objectId,initialSharedVersion:o.owner.Shared.initialSharedVersion,mutable});}
  for(const pkg of [base.pkg,head]){
   const gas=(await client.listCoins({owner:device.toSuiAddress()})).objects[0];
   tx=new Transaction();tx.setSender(device.toSuiAddress());tx.setGasPayment([{objectId:gas.objectId,version:gas.version,digest:gas.digest}]);
   tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice);tx.setGasBudget(50_000_000);
   const cap=(await client.getObject({objectId:deviceCap})).object;
   tx.moveCall({target:`${pkg}::fleet_data::report_health`,arguments:[await shared(tx,fleet,false),await shared(tx,writer,true),await shared(tx,budget,false),await shared(tx,account,false),await shared(tx,registry,false),tx.objectRef({objectId:deviceCap,version:cap.version,digest:cap.digest}),tx.pure.u64(1),tx.pure.vector('u8',[1]),tx.pure.vector('u8',Array(97).fill(0)),await shared(tx,'0x6',false)]});
   const bytes=await tx.build(),signed=await device.signTransaction(bytes);
   const result=await client.executeTransaction({transaction:bytes,signatures:[signed.signature],include:{effects:true}});
   assert.equal(result.$kind,'FailedTransaction');assert.match(JSON.stringify(result.FailedTransaction.status),/device_cap::assert_active/);
   await client.waitForTransaction({digest:result.FailedTransaction.digest});attacks[pkg]={digest:result.FailedTransaction.digest,status:result.FailedTransaction.status};
  }
  console.log(JSON.stringify({result:'PASS',chain,baseRef,base:base.pkg,head,origins,digests,attacks,scopedChecks,checks:['pinned real Walrus/storage dependencies','existing-object byte preservation','later DiagnosticKey origin','new API on existing writer','old and new entry points refuse revoked device']}));
 }finally{await fs.rm(tmp,{recursive:true,force:true});}
})().catch(e=>{console.error(e.stack);process.exitCode=1});
