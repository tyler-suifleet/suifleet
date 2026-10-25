// SF-136 acceptance on a network-none disposable chain. Real base->head
// upgrade, populated adoption, old-package attacks, wallet revocation and reads.
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const { appRequire, loader } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519"), { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet");
const { decodeSuiPrivateKey } = appRequire("@mysten/sui/cryptography");
const { bcs } = appRequire("@mysten/sui/bcs"), { fromBase58, toHex } = appRequire("@mysten/sui/utils");
const root=path.resolve(__dirname,".."), rpc="http://127.0.0.1:9000", faucet="http://127.0.0.1:9123";
(async()=>{
 assert.deepEqual((await fs.readdir("/sys/class/net")).sort(),["lo"]);
 const tmp=await fs.mkdtemp(path.join(os.tmpdir(),"sf136-customer-"));
 let daemon;
 async function stopDaemon(){if(daemon && daemon.exitCode===null){const stopped=new Promise(resolve=>daemon.once("exit",resolve));daemon.kill("SIGTERM");await stopped;}daemon=undefined;}
 try {
  const base=execFileSync("git",["merge-base","HEAD","origin/main"],{encoding:"utf8"}).trim();
  const oldTree=path.join(tmp,"base"); await fs.mkdir(oldTree);
  execFileSync("tar",["-x","-C",oldTree],{input:execFileSync("git",["archive",base,"contracts"],{maxBuffer:20*1024*1024})});
  assert.ok(!(await fs.readdir(path.join(oldTree,"contracts/sources"))).includes("customer.move"),"base must precede customer introduction");
  const env={...process.env,SUI_CONFIG_DIR:path.join(tmp,"wallet")};await fs.mkdir(env.SUI_CONFIG_DIR);
  const keystore=path.join(env.SUI_CONFIG_DIR,"empty.keystore");await fs.writeFile(keystore,"[]");
  await fs.writeFile(path.join(env.SUI_CONFIG_DIR,"client.yaml"),`keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: ${rpc}\nactive_env: testnet\nactive_address: null\n`);
  function compile(directory){
   let text;try{text=execFileSync("sui",["move","build","--path",directory,"--build-env","testnet","--no-tree-shaking","--dump-bytecode-as-base64"],{env,encoding:"utf8",maxBuffer:25*1024*1024,stdio:["ignore","pipe","pipe"]});}catch(e){throw Error(`compile failed (${path.basename(directory)}): ${String(e.stderr).slice(-1500)}`);}
   return JSON.parse(text.slice(text.indexOf("{")));
  }
  const client=new SuiGrpcClient({network:"localnet",baseUrl:rpc});
  const owner=Ed25519Keypair.generate(), delegate=Ed25519Keypair.generate(), device=Ed25519Keypair.generate();
  for(const signer of [...Array(12).fill(owner),delegate,device]) { for(let retry=0;;retry++) { try { await requestSuiFromFaucetV2({host:faucet,recipient:signer.toSuiAddress()}); break; } catch(error) { if(retry===9) throw error; await new Promise(resolve=>setTimeout(resolve,500)); } } }
  const digests={},failures={};
  async function execute(label,tx,signer=owner){
   tx.setGasBudget(label === "upgrade head" ? 2_000_000_000 : 400_000_000);
   let r;try{r=await client.signAndExecuteTransaction({transaction:tx,signer,include:{effects:true,events:true,objectTypes:true}});}catch(e){throw Error(`${label}: ${e.message}`);}
   assert.ok(r.Transaction?.status.success,`${label}: ${JSON.stringify(r.FailedTransaction?.status)}`);
   await client.waitForTransaction({digest:r.Transaction.digest});digests[label]=r.Transaction.digest;console.log(`PASS localnet: ${label}`);return r.Transaction;
  }
  const created=(r,suffix)=>{const id=Object.keys(r.objectTypes).find(id=>r.objectTypes[id].endsWith(suffix));assert.ok(id,`missing ${suffix}`);return id;};
  const expectedErrors = {
   "retired pending account cannot release capacity twice": [1, "customer::retire_pending_device"],
   "pending migration cannot lend cap": [1, "customer::borrow_budget_cap"],
   "old group mutation after adoption": [0, "device_group::remove_device"],
   "old budget mutation with borrowed cap": /InvalidLinkage|invalid linkage/i,
   "old gas draw after adoption": [0, "fleet_budget::assert_may_draw"],
   "cross fleet onchain rollout": [1, "customer::advance_rollout_onchain"],
   "revoked wallet advances rollout": [0, "customer::assert_member"],
   "cross fleet group insertion": [1, "customer::assert_group"],
   "revoked wallet creates fleet": [0, "customer::assert_member"],
   "old receipt mutation after adoption": [5, "deployment_manager::receipts_mut"],
   "reassigned device cannot report source receipt": [1, "customer::assert_reporter"],
  };
  async function reject(label,tx,signer=owner){
   const gas=(await client.listCoins({owner:signer.toSuiAddress()})).objects[0];
   tx.setSender(signer.toSuiAddress());tx.setGasPayment([{objectId:gas.objectId,version:gas.version,digest:gas.digest}]);tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice);tx.setGasBudget(50_000_000);
   // Resolve from the reviewed ABI, including exact shared mutability, so
   // refusal is signed execution effects rather than resolver simulation.
   const abi=JSON.parse(await fs.readFile("interfaces/fleet-v1.json","utf8")).modules;
   const data=tx.getData(), mutable=new Set();
   for(const command of data.commands) if(command.MoveCall) {
    const call=command.MoveCall, parameters=abi[call.module].functions[call.function].parameters;
    call.arguments.forEach((arg,i)=>{if(arg.$kind==="Input" && parameters[i].startsWith("&mut ")) mutable.add(arg.Input);});
   }
   for(let i=0;i<data.inputs.length;i++) {
    const id=data.inputs[i].UnresolvedObject?.objectId;if(!id)continue;
    const {object}=await client.getObject({objectId:id});
    data.inputs[i]=object.owner.$kind==="Shared"?{Object:{SharedObject:{objectId:id,initialSharedVersion:object.owner.Shared.initialSharedVersion,mutable:mutable.has(i)}}}:{Object:{ImmOrOwnedObject:{objectId:id,version:object.version,digest:object.digest}}};
   }
   const bytes=await Transaction.from(JSON.stringify(data)).build(), signed=await signer.signTransaction(bytes);
   const r=await client.executeTransaction({transaction:bytes,signatures:[signed.signature],include:{effects:true}});
   assert.ok(r.FailedTransaction,`${label} unexpectedly succeeded`);await client.waitForTransaction({digest:r.FailedTransaction.digest});
   const status=r.FailedTransaction.status.error?.message ?? JSON.stringify(r.FailedTransaction.status);
   const expected=expectedErrors[label];assert.ok(expected,`missing failure expectation: ${label}`);
   assert.match(status,Array.isArray(expected)?new RegExp(`abort code: ${expected[0]}, in '[^']*::${expected[1]}'`):expected,`${label}: ${status}`);
   failures[label]={digest:r.FailedTransaction.digest,status};
  }
  let tx=new Transaction();const [publishCap]=tx.publish(compile(path.join(oldTree,"contracts")));tx.transferObjects([publishCap],owner.toSuiAddress());
  const published=await execute("publish base",tx), old=published.effects.changedObjects.find(o=>o.outputState==="PackageWrite").objectId;
  const registry=created(published,"::device_registry::DeviceRegistry"), upgradeCap=created(published,"::package::UpgradeCap");
  tx=new Transaction();const [deposit]=tx.splitCoins(tx.gas,[tx.pure.u64(200_000_000)]);
  const [limits]=tx.moveCall({target:`${old}::fleet_budget::new_limits`,arguments:[60_000_000,86400000,250_000_000,6,0].map(n=>tx.pure.u64(n))});
  const [budgetCap]=tx.moveCall({target:`${old}::fleet_budget::create`,arguments:[tx.object(registry),deposit,limits,tx.pure.u64(0)]});tx.transferObjects([budgetCap],owner.toSuiAddress());
  const br=await execute("legacy budget",tx), budget=created(br,"::fleet_budget::FleetBudget"), cap=created(br,"::fleet_budget::BudgetOwnerCap");
  tx=new Transaction();const [d]=tx.moveCall({target:`${old}::device_registry::register_device`,arguments:[tx.object(registry),tx.pure.string("Existing device"),tx.pure.address(device.toSuiAddress()),tx.pure.string("x86_64"),tx.object("0x6")]});
  tx.moveCall({target:`${old}::device_cap::issue_cap`,arguments:[d]});const [oc]=tx.moveCall({target:`${old}::device_registry::activate_device`,arguments:[tx.object(registry),d]});tx.transferObjects([oc],owner.toSuiAddress());
  const dr=await execute("legacy device",tx), deviceId=created(dr,"::device_registry::Device"), ownerCap=created(dr,"::device_registry::DeviceOwnerCap"), deviceCap=created(dr,"::device_cap::DeviceCap");
  tx=new Transaction();tx.moveCall({target:`${old}::fleet_budget::enroll_device`,arguments:[tx.object(budget),tx.object(cap),tx.object(registry),tx.object(ownerCap),tx.pure.u64(0),tx.pure.u64(100_000_000),tx.object("0x6")]});const legacyAccount=created(await execute("legacy enrollment",tx),"::fleet_budget::GasAccount");
  tx=new Transaction();tx.moveCall({target:`${old}::device_group::create_group`,arguments:[tx.pure.string("Existing group"),tx.pure.string(""),tx.object("0x6")]});const group=created(await execute("legacy group",tx),"::device_group::DeviceGroup");
  tx=new Transaction();const [borrowed,loan]=tx.moveCall({target:`${old}::device_registry::borrow_device`,arguments:[tx.object(registry),tx.object(ownerCap)]});tx.moveCall({target:`${old}::device_group::add_device`,arguments:[tx.object(group),borrowed]});tx.moveCall({target:`${old}::device_registry::return_device`,arguments:[tx.object(registry),borrowed,loan]});await execute("legacy membership",tx);
  // Ordinary legacy deregistration leaves an indexed budget account behind.
  tx=new Transaction();const [staleDevice]=tx.moveCall({target:`${old}::device_registry::register_device`,arguments:[tx.object(registry),tx.pure.string("Deregistered legacy device"),tx.pure.address(delegate.toSuiAddress()),tx.pure.string("x86_64"),tx.object("0x6")]});
  const [staleCap]=tx.moveCall({target:`${old}::device_registry::activate_device`,arguments:[tx.object(registry),staleDevice]});tx.transferObjects([staleCap],owner.toSuiAddress());
  const stale=await execute("legacy stale device",tx), staleOwnerCap=created(stale,"::device_registry::DeviceOwnerCap");
  tx=new Transaction();tx.moveCall({target:`${old}::fleet_budget::enroll_device`,arguments:[tx.object(budget),tx.object(cap),tx.object(registry),tx.object(staleOwnerCap),tx.pure.u64(0),tx.pure.u64(1000000),tx.object("0x6")]});
  const staleAccount=created(await execute("legacy stale enrollment",tx),"::fleet_budget::GasAccount");
  tx=new Transaction();tx.moveCall({target:`${old}::device_registry::deregister_activated_device`,arguments:[tx.object(registry),tx.object(staleOwnerCap)]});await execute("legacy deregistration leaves account",tx);
  const built=compile(path.join(root,"contracts"));tx=new Transaction();const [ticket]=tx.moveCall({target:"0x2::package::authorize_upgrade",arguments:[tx.object(upgradeCap),tx.pure.u8(0),tx.pure.vector("u8",built.digest)]});const receipt=tx.upgrade({modules:built.modules,dependencies:built.dependencies,package:old,ticket});tx.moveCall({target:"0x2::package::commit_upgrade",arguments:[tx.object(upgradeCap),receipt]});
  const upgraded=await execute("upgrade head",tx), head=upgraded.effects.changedObjects.find(o=>o.outputState==="PackageWrite").objectId;
  const pkg=bcs.Object.parse((await client.getObject({objectId:head,include:{objectBcs:true}})).object.objectBcs).data.Package;
  const origins=Object.fromEntries(pkg.typeOriginTable.map(o=>[`${o.moduleName}::${o.datatypeName}`,o.package]));
  assert.equal(origins["device_registry::Device"],old);assert.equal(origins["customer::Customer"],head);assert.equal(origins["fleet_budget::FleetBudget"],old);
  const call=(name,args)=>{const t=new Transaction();t.moveCall({target:`${head}::customer::${name}`,arguments:args(t)});return t;};
  const r=await execute("create customer and default",call("create",t=>[t.object(registry),t.pure.string("Customer")]));const customer=created(r,"::customer::Customer");
  await execute("retry customer",call("create",t=>[t.object(registry),t.pure.string("Customer")]));
  await execute("set entitlement",call("set_entitlement",t=>[t.object(customer),t.object(registry),t.pure.u64(2),t.pure.u64(10000000),t.pure.u64(1)]));
  const ar=await execute("adopt populated budget",call("adopt_budget",t=>[t.object(customer),t.object(budget),t.object(cap),t.pure.string("Production")]));const fleet=created(ar,"::customer::Fleet");
  tx=new Transaction();const [bc,bl]=tx.moveCall({target:`${head}::customer::borrow_budget_cap`,arguments:[tx.object(customer),tx.object(fleet)]});tx.moveCall({target:`${head}::customer::return_budget_cap`,arguments:[tx.object(fleet),bc,bl]});await reject("pending migration cannot lend cap",tx);
  await execute("reconcile device",call("adopt_device",t=>[t.object(customer),t.object(fleet),t.object(budget),t.object(legacyAccount),t.object(registry),t.object(ownerCap)]));
  await execute("retire stale pending account",call("retire_pending_device",t=>[t.object(customer),t.object(fleet),t.object(budget),t.object(staleAccount)]));
  await reject("retired pending account cannot release capacity twice",call("retire_pending_device",t=>[t.object(customer),t.object(fleet),t.object(budget),t.object(staleAccount)]));
  await execute("adopt populated group",call("adopt_group",t=>[t.object(customer),t.object(fleet),t.object(group)]));
  tx=new Transaction();tx.moveCall({target:`${old}::fleet_budget::draw_gas`,arguments:[tx.object(budget),tx.object(legacyAccount),tx.object(registry),tx.object(deviceCap),tx.pure.u64(1000000),tx.object("0x6")]});await reject("old gas draw after adoption",tx,device);
  tx=new Transaction();tx.moveCall({target:`${old}::device_group::remove_device`,arguments:[tx.object(group),tx.pure.id(deviceId)]});await reject("old group mutation after adoption",tx);
  tx=new Transaction();const [borrowCap,borrowLoan]=tx.moveCall({target:`${head}::customer::borrow_budget_cap`,arguments:[tx.object(customer),tx.object(fleet)]});tx.moveCall({target:`${old}::fleet_budget::set_paused`,arguments:[tx.object(budget),borrowCap,tx.pure.bool(true)]});tx.moveCall({target:`${head}::customer::return_budget_cap`,arguments:[tx.object(fleet),borrowCap,borrowLoan]});await reject("old budget mutation with borrowed cap",tx);
  await execute("add second wallet",call("set_credential",t=>[t.object(customer),t.pure.address(delegate.toSuiAddress()),t.pure.bool(true),t.pure.bool(true)]));
  const second=created(await execute("second wallet creates fleet",call("create_fleet",t=>[t.object(customer),t.object(registry),t.pure.string("Production")]),delegate),"::customer::Fleet");
  await reject("cross fleet group insertion",call("add_to_group",t=>[t.object(customer),t.object(second),t.object(group),t.pure.id(deviceId)]),delegate);
  await execute("revoke second wallet",call("set_credential",t=>[t.object(customer),t.pure.address(delegate.toSuiAddress()),t.pure.bool(false),t.pure.bool(false)]));
  await reject("revoked wallet creates fleet",call("create_fleet",t=>[t.object(customer),t.object(registry),t.pure.string("Bypass")]),delegate);
  // Existing device draws still work through the new code after adoption.
  const account=await client.getDynamicField({parentId:budget,name:{type:`${old}::fleet_budget::DeviceKey`,bcs:bcs.Address.serialize(deviceId).toBytes()}});const accountId=bcs.Address.parse(account.dynamicField.value.bcs);
  tx=new Transaction();tx.moveCall({target:`${head}::fleet_budget::draw_gas`,arguments:[tx.object(budget),tx.object(accountId),tx.object(registry),tx.object(deviceCap),tx.pure.u64(1000000),tx.object("0x6")]});await execute("managed device gas draw",tx,device);
  const manifest=JSON.parse(await fs.readFile("manifests/fixture.localnet.json","utf8"));manifest.network="localnet";manifest.rpc_url=rpc;manifest.call_package_id=head;manifest.upgrade_cap_id=upgradeCap;manifest.package_version=String(pkg.version);manifest.type_origins=origins;
  manifest.chain_identifier=toHex(fromBase58((await client.ledgerService.getServiceInfo({})).response.chainId).slice(0,4));
  manifest.shared_objects.registry={id:registry,initial_shared_version:(await client.getObject({objectId:registry})).object.owner.Shared.initialSharedVersion};
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
  const read=loader()("lib/customer-fleets.ts");const discovered=await read.fetchCustomerFleets(client,owner.toSuiAddress());assert.equal(discovered.length,3);assert.equal(discovered.find(f=>f.id===fleet).activeDevices,1n);assert.deepEqual(await read.fetchCustomerFleets(client,delegate.toSuiAddress()),[]);
  // Exercise the real daemon custody resolver, not a browser-only authority model.
  const manifestPath=path.join(tmp,"manifest.json");await fs.writeFile(manifestPath,JSON.stringify(manifest));const key=path.join(tmp,"device.key");await fs.writeFile(key,Buffer.from(decodeSuiPrivateKey(device.getSecretKey()).secretKey).toString("hex"),{mode:0o600});
  const cfg=path.join(tmp,"config.toml");await fs.writeFile(cfg,`[sui]\nmanifest_path=${JSON.stringify(manifestPath)}\n[device]\nkeypair_path=${JSON.stringify(key)}\ndevice_object_id="${deviceId}"\ndevice_cap_id="${deviceCap}"\n[apply]\ncommand="/bin/false"\ntimeout_seconds=5\n[runtime]\nstate_dir=${JSON.stringify(path.join(tmp,"state"))}\n`);
  const operator=execFileSync("suifleet-edge/target/debug/suifleet",["--check-operator",cfg],{encoding:"utf8",stdio:["ignore","pipe","pipe"]});assert.ok(operator.includes(fleet),"daemon did not resolve the managed fleet operator");
  // Use the reviewed settings API with a real capability loan on chain.
  const load=loader(), settings=load("lib/fleet-settings.ts");
  await execute("managed settings capability composition",read.withManagedBudgetCap(settings.buildSetFleetSettingsTx(budget,cap,settings.DEFAULT_SETTINGS),discovered.find(f=>f.id===fleet),cap));
  const deployed=await execute("managed receipt deployment",call("create_deployment",t=>[t.object(customer),t.object(fleet),t.pure.string("app"),t.pure.string("1"),t.pure.string("unavailable-recipe"),t.pure.string("a".repeat(64)),t.pure.string("b".repeat(64)),t.pure.vector("id",[]),t.pure.vector("id",[deviceId]),t.object("0x6")]));
  const deployment=created(deployed,"::deployment_manager::DeploymentRecord");
  tx=new Transaction();tx.moveCall({target:`${old}::deployment_manager::report_verified_status`,arguments:[tx.object(deployment),tx.object(registry),tx.object(deviceCap),tx.pure.u8(1)]});await reject("old receipt mutation after adoption",tx,device);
  // No updater/server: an unavailable recipe produces a real Failed receipt.
  manifest.walrus.aggregator_url="http://127.0.0.1:1";await fs.writeFile(manifestPath,JSON.stringify(manifest));
  await fs.appendFile(cfg,"poll_seconds=1\nreplay_history=true\n");
  const log=await fs.open(path.join(tmp,"daemon.log"),"w",0o600);
  daemon=spawn("suifleet-edge/target/debug/suifleet",[],{env:{...env,EDGE_CONFIG:cfg,RUST_LOG:"suifleet=info"},stdio:["ignore",log.fd,log.fd]});await log.close();
  const receiptsBcs=load("lib/move-bcs.ts").ReceiptsBcs;
  let reported;
  for(let attempt=0;attempt<90;attempt++){
   const field=await client.getDynamicField({parentId:deployment,name:{type:`${old}::deployment_manager::ReceiptsKey`,bcs:bcs.bool().serialize(false).toBytes()}});
   const receipt=receiptsBcs.parse(field.dynamicField.value.bcs);reported=receipt.statuses.contents.find(row=>row.key===deviceId);
   if(reported) {assert.equal(receipt.version,"3");assert.equal(reported.value,2);break;}
   if(daemon.exitCode!==null)throw Error("daemon exited before its managed receipt (private log retained only in temporary runtime)");
   await new Promise(resolve=>setTimeout(resolve,500));
  }
  assert.ok(reported,"real daemon did not submit its managed Failed receipt");await stopDaemon();
  console.log("PASS localnet: real daemon signed and submitted a managed receipt with checked chain state");
  tx=new Transaction();
  const [rolloutPolicy]=tx.moveCall({target:`${head}::deployment_manager::rollout_policy`,arguments:[tx.pure.u64(1),tx.pure.u64(1),tx.pure.u64(1),tx.pure.u64(100),tx.pure.u64(1),tx.pure.u64(10000),tx.pure.u64(200000000)]});
  const [rolloutRecord]=tx.moveCall({target:`${head}::customer::new_rollout`,arguments:[tx.object(customer),tx.object(fleet),tx.pure.string("app"),tx.pure.string("1"),tx.pure.string("blob"),tx.pure.string("a".repeat(64)),tx.pure.string("b".repeat(64)),tx.pure.vector("id",[deviceId]),tx.pure.vector("u64",[0]),rolloutPolicy,tx.object("0x6")]});
  tx.moveCall({target:`${head}::deployment_manager::share_deployment`,arguments:[rolloutRecord]});
  const rolloutId=created(await execute("managed onchain rollout creation",tx),"::deployment_manager::DeploymentRecord");
  const tick=(selected,candidates)=>call("advance_rollout_onchain",t=>[t.object(customer),t.object(selected),t.object(rolloutId),t.object(registry),t.pure.vector("id",candidates),t.object("0x6")]);
  await execute("managed onchain rollout admission",tick(fleet,[deviceId]));
  await execute("managed onchain rollout no fabricated health",tick(fleet,[]));
  const rolloutState=await client.getDynamicField({parentId:rolloutId,name:{type:`${old}::deployment_manager::RolloutKey`,bcs:bcs.bool().serialize(false).toBytes()}});
  const protocol=loader()("lib/fleet-protocol.ts");
  assert.equal(protocol.deployment_manager_RolloutBcs.parse(rolloutState.dynamicField.value.bcs).state,0);
  await reject("cross fleet onchain rollout",tick(second,[]));
  await reject("revoked wallet advances rollout",tick(fleet,[]),delegate);
  await execute("clear source group before reassignment",call("remove_from_group",t=>[t.object(customer),t.object(fleet),t.object(group),t.pure.id(deviceId)]));
  const secondView=discovered.find(f=>f.id===second);
  await execute("explicit atomic reassignment",call("reassign_device",t=>[t.object(customer),t.object(fleet),t.object(second),t.object(budget),t.object(secondView.budgetId),t.object(accountId),t.object(registry),t.pure.id(deviceId),t.object("0x6")]));
  await reject("reassigned device cannot report source receipt",call("report_status",t=>[t.object(customer),t.object(fleet),t.object(deployment),t.object(registry),t.object(deviceCap),t.pure.u8(1)]),device);
  const after=await read.fetchCustomerFleets(client,owner.toSuiAddress());assert.equal(after.find(f=>f.id===second).activeDevices,1n);assert.ok(after.find(f=>f.id===second).devices.includes(deviceId));assert.ok(!after.find(f=>f.id===fleet).devices.includes(deviceId));
  const operatorAfter=execFileSync("suifleet-edge/target/debug/suifleet",["--check-operator",cfg],{encoding:"utf8",stdio:["ignore","pipe","pipe"]});assert.ok(operatorAfter.includes(second));
  const budgetUi=load("lib/fleet-budget.ts");
  const destinationAccount=await client.getDynamicField({parentId:secondView.budgetId,name:{type:`${old}::fleet_budget::DeviceKey`,bcs:bcs.Address.serialize(deviceId).toBytes()}});
  const destinationAccountId=bcs.Address.parse(destinationAccount.dynamicField.value.bcs);
  const removalResult=await execute("managed removal returns custody",budgetUi.buildRemoveDeviceTx(secondView.budgetId,cap,destinationAccountId,secondView,deviceId,owner.toSuiAddress()));
  const released=await client.getObject({objectId:created(removalResult,"::device_registry::DeviceOwnerCap")});
  assert.equal(released.object.owner.AddressOwner,owner.toSuiAddress());
  const removed=await read.fetchCustomerFleets(client,owner.toSuiAddress());
  assert.equal(removed.find(f=>f.id===second).activeDevices,0n);
  assert.ok(!removed.find(f=>f.id===second).devices.includes(deviceId));
  console.log(JSON.stringify({result:"PASS",base,old_package:old,head_package:head,customer,fleet,digests,failures,daemon_operator:JSON.parse(operator.trim().split("\n").at(-1)).operator,operator_after_reassignment:second,managed_receipt:reported}));

 } finally {await stopDaemon();await fs.rm(tmp,{recursive:true,force:true});}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
