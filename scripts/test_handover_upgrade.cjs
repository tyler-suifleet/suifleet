// Existing-object base->head compatible upgrade; real old-package attack effects.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict'),{execFileSync}=require('node:child_process');
const {appRequire}=require('./frontend_test_utils.cjs');
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'),{Transaction}=appRequire('@mysten/sui/transactions'),{Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519'),{requestSuiFromFaucetV2}=appRequire('@mysten/sui/faucet');
const rpc='http://127.0.0.1:9000',faucet='http://127.0.0.1:9123';
(async()=>{
  assert.deepEqual((await fs.readdir('/sys/class/net')).sort(),['lo']);
  const tmp=await fs.mkdtemp(path.join(os.tmpdir(),'sf089-upgrade-'));
  try {
    const base=execFileSync('git',['merge-base','HEAD','origin/main'],{encoding:'utf8'}).trim();
    const oldTree=path.join(tmp,'base');await fs.mkdir(oldTree);
    const archive=execFileSync('git',['archive',base,'contracts'],{maxBuffer:20*1024*1024});
    execFileSync('tar',['-x','-C',oldTree],{input:archive});
    const env={...process.env,SUI_CONFIG_DIR:path.join(tmp,'wallet')};await fs.mkdir(env.SUI_CONFIG_DIR);
    const keystore=path.join(env.SUI_CONFIG_DIR,'sui.keystore');await fs.writeFile(keystore,'[]');
    await fs.writeFile(path.join(env.SUI_CONFIG_DIR,'client.yaml'),`keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: ${rpc}\nactive_env: testnet\nactive_address: null\n`);
    function compile(directory){let text;try{text=execFileSync('sui',['move','build','--path',directory,'--build-env','testnet','--no-tree-shaking','--dump-bytecode-as-base64'],{env,encoding:'utf8',maxBuffer:20*1024*1024,stdio:['ignore','pipe','pipe']});}catch{throw Error('isolated compilation failed; wallet output suppressed');}return JSON.parse(text.slice(text.indexOf('{')));}
    const client=new SuiGrpcClient({network:'localnet',baseUrl:rpc}),source=Ed25519Keypair.generate(),recipient=Ed25519Keypair.generate();
    for(const signer of [source,recipient]) {
      for(let attempt=0;attempt<30;attempt++){
        try {await requestSuiFromFaucetV2({host:faucet,recipient:signer.toSuiAddress()});break;}
        catch(error){if(attempt===29)throw error;await new Promise(resolve=>setTimeout(resolve,1000));}
      }
    }
    const digests=[];
    async function execute(transaction,signer=source){const r=await client.signAndExecuteTransaction({transaction,signer,include:{effects:true,objectTypes:true}});assert.ok(r.Transaction?.status.success,JSON.stringify(r.FailedTransaction?.status));await client.waitForTransaction({digest:r.Transaction.digest});digests.push(r.Transaction.digest);return r.Transaction;}
    const created=(r,suffix)=>Object.keys(r.objectTypes).find(id=>r.objectTypes[id].endsWith(suffix));
    let tx=new Transaction();let [cap]=tx.publish(compile(path.join(oldTree,'contracts')));tx.transferObjects([cap],source.toSuiAddress());
    const first=await execute(tx),old=first.effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId,upgradeCap=created(first,'::package::UpgradeCap');
    tx=new Transaction();tx.moveCall({target:`${old}::device_group::create_group`,arguments:[tx.pure.string('pre-upgrade fleet'),tx.pure.string(''),tx.object('0x6')]});
    const group=created(await execute(tx),'::device_group::DeviceGroup');
    tx=new Transaction();tx.moveCall({target:`${old}::deployment_manager::create_deployment_verified`,arguments:[tx.pure.string('app'),tx.pure.string('1.0.0'),tx.pure.string('blob'),tx.pure.string('ab'.repeat(32)),tx.pure.string('cd'.repeat(32)),tx.pure.vector('address',[group]),tx.pure.vector('address',[]),tx.object('0x6')]});
    const record=created(await execute(tx),'::deployment_manager::DeploymentRecord');
    const headTree=path.join(tmp,'head');await fs.mkdir(headTree);await fs.cp(path.resolve('contracts/sources'),path.join(headTree,'sources'),{recursive:true});
    for(const name of ['Move.toml','Move.lock'])await fs.copyFile(path.resolve('contracts',name),path.join(headTree,name));
    const built=compile(headTree);tx=new Transaction();const [ticket]=tx.moveCall({target:'0x2::package::authorize_upgrade',arguments:[tx.object(upgradeCap),tx.pure.u8(0),tx.pure.vector('u8',built.digest)]});
    const receipt=tx.upgrade({modules:built.modules,dependencies:built.dependencies,package:old,ticket});
    tx.moveCall({target:'0x2::package::commit_upgrade',arguments:[tx.object(upgradeCap),receipt]});
    const upgraded=await execute(tx),head=upgraded.effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId;
    const {bcs}=appRequire('@mysten/sui/bcs');
    const raw=bcs.Object.parse((await client.getObject({objectId:head,include:{objectBcs:true}})).object.objectBcs).data.Package;
    const origins=Object.fromEntries(raw.typeOriginTable.map(o=>[`${o.moduleName}::${o.datatypeName}`,o.package]));
    assert.equal(origins['device_group::DeviceGroup'],old);assert.equal(origins['deployment_manager::DeploymentRecord'],old);assert.equal(origins['fleet_handover::Handover'],head);
    tx=new Transaction();tx.moveCall({target:`${head}::fleet_handover::propose`,arguments:[tx.pure.address(recipient.toSuiAddress()),tx.pure.vector('address',[group,record]),tx.pure.vector('u64',[1,2]),tx.pure.u64(Date.now()+3600000),tx.object('0x6')]});
    const handover=created(await execute(tx),'::fleet_handover::Handover');
    tx=new Transaction();tx.moveCall({target:`${head}::fleet_handover::confirm`,arguments:[tx.object(handover),tx.object('0x6')]});await execute(tx,recipient);
    tx=new Transaction();for(const [fn,id]of [['group',group],['deployment',record]])tx.moveCall({target:`${head}::fleet_handover::${fn}`,arguments:[tx.object(handover),tx.object(id),tx.object('0x6')]});await execute(tx);
    // Fully resolved bytes bypass simulation, so attacks produce chain effects.
    for(const [module,fn,id,args]of [['device_group','remove_device',group,['address','0x99']],['deployment_manager','set_control',record,null]]){
      const object=(await client.getObject({objectId:id})).object,gas=(await client.listCoins({owner:source.toSuiAddress()})).objects[0],price=(await client.getReferenceGasPrice()).referenceGasPrice;
      tx=new Transaction();tx.setSender(source.toSuiAddress());tx.setGasPayment([{objectId:gas.objectId,version:gas.version,digest:gas.digest}]);tx.setGasPrice(price);tx.setGasBudget(100000000);
      const extra=args?[tx.pure.address(args[1])]:[tx.pure.u8(0),...Array.from({length:5},()=>tx.pure.u64(0))];
      tx.moveCall({target:`${old}::${module}::${fn}`,arguments:[tx.sharedObjectRef({objectId:id,initialSharedVersion:object.owner.Shared.initialSharedVersion,mutable:true}),...extra]});
      const bytes=await tx.build(),signed=await source.signTransaction(bytes),result=await client.executeTransaction({transaction:bytes,signatures:[signed.signature],include:{effects:true}});
      assert.equal(result.$kind,'FailedTransaction');await client.waitForTransaction({digest:result.FailedTransaction.digest});digests.push(result.FailedTransaction.digest);
    }
    tx=new Transaction();tx.moveCall({target:`${head}::fleet_handover::finish`,arguments:[tx.object(handover)]});await execute(tx,recipient);
    // Old entry points now recognize the recipient, too: no package-version
    // fiction substitutes for updating the underlying authority fields.
    tx=new Transaction();tx.moveCall({target:`${old}::deployment_manager::set_control`,arguments:[tx.object(record),tx.pure.u8(1),...Array.from({length:5},()=>tx.pure.u64(0))]});await execute(tx,recipient);
    console.log(JSON.stringify({result:'PASS',base,old_package:old,head_package:head,origins:{group:origins['device_group::DeviceGroup'],record:origins['deployment_manager::DeploymentRecord'],handover:origins['fleet_handover::Handover']},digests,checks:['compatible base-to-head upgrade','existing shared group and record','new handover origin','recipient confirmation','old-package old-owner attempts failed on chain','recipient controls original record through old package']}));
  } finally {await fs.rm(tmp,{recursive:true,force:true});}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
