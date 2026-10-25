// SF-127: real pre-identity upgrade/deletion attack and daemon/indexer
// interoperability for ordinary and schema-2 catalog sharded receipts.
// Run through scripts/test_protocol_localnet.py --receipt-clients (network none).
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const { appRequire, loader } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519"), { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet");
const { bcs } = appRequire("@mysten/sui/bcs"), { normalizeSuiAddress } = appRequire("@mysten/sui/utils");
const rpc = "http://127.0.0.1:9000", faucet = "http://127.0.0.1:9123", root = path.resolve(__dirname, "..");
const DAEMON = path.join(root, "suifleet-edge/target/debug/suifleet");
const SUI = 1_000_000_000n, MINUTE = 60_000n, DAY = 86_400_000n;

(async () => {
  assert.deepEqual((await fs.readdir("/sys/class/net")).sort(), ["lo"]);
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "sf127-receipts-"));
  let daemon;
  try {
    const base = execFileSync("git", ["rev-parse", "f43d531^1"], { encoding: "utf8" }).trim();
    const oldTree = path.join(tmp, "base"); await fs.mkdir(oldTree);
    execFileSync("tar", ["-x", "-C", oldTree], { input: execFileSync("git", ["archive", base, "contracts"], { maxBuffer: 20 * 1024 * 1024 }) });
    const env = { ...process.env, SUI_CONFIG_DIR: path.join(tmp, "wallet") }; await fs.mkdir(env.SUI_CONFIG_DIR);
    const keystore = path.join(env.SUI_CONFIG_DIR, "sui.keystore"); await fs.writeFile(keystore, "[]");
    await fs.writeFile(path.join(env.SUI_CONFIG_DIR, "client.yaml"), `keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: ${rpc}\nactive_env: testnet\nactive_address: null\n`);
    function compile(directory) {
      let text;
      try { text = execFileSync("sui", ["move", "build", "--path", directory, "--build-env", "testnet", "--no-tree-shaking", "--dump-bytecode-as-base64"], { env, encoding: "utf8", maxBuffer: 20 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] }); }
      catch { throw Error("isolated compilation failed; wallet output suppressed"); }
      return JSON.parse(text.slice(text.indexOf("{")));
    }
    const client = new SuiGrpcClient({ network: "localnet", baseUrl: rpc });
    const owner = Ed25519Keypair.generate(), stranger = Ed25519Keypair.generate();
    for (const signer of [owner, stranger]) {
      for (let attempt = 0; attempt < 30; attempt++) {
        try { await requestSuiFromFaucetV2({ host: faucet, recipient: signer.toSuiAddress() }); break; }
        catch (error) { if (attempt === 29) throw error; await new Promise((resolve) => setTimeout(resolve, 1000)); }
      }
    }
    const digests = {}, failures = {};
    async function execute(label, transaction, signer = owner) {
      const r = await client.signAndExecuteTransaction({ transaction, signer, include: { effects: true, objectTypes: true, events: true } });
      assert.ok(r.Transaction?.status.success, `${label}: ${JSON.stringify(r.FailedTransaction?.status)}`);
      await client.waitForTransaction({ digest: r.Transaction.digest });
      digests[label] = r.Transaction.digest;
      return r.Transaction;
    }
    const created = (r, suffix) => Object.keys(r.objectTypes).find((id) => r.objectTypes[id].endsWith(suffix));
    // Fully resolved bytes bypass simulation, so a refused call fails with chain effects.
    async function attack(label, signer, build, abort) {
      const gas = (await client.listCoins({ owner: signer.toSuiAddress() })).objects[0];
      const tx = new Transaction(); tx.setSender(signer.toSuiAddress());
      tx.setGasPayment([{ objectId: gas.objectId, version: gas.version, digest: gas.digest }]);
      tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice); tx.setGasBudget(50_000_000);
      await build(tx);
      const bytes = await tx.build(), signed = await signer.signTransaction(bytes);
      const result = await client.executeTransaction({ transaction: bytes, signatures: [signed.signature], include: { effects: true } });
      assert.equal(result.$kind, "FailedTransaction", `${label} succeeded`);
      const status = JSON.stringify(result.FailedTransaction.status);
      assert.match(status, abort, `${label}: ${status}`);
      await client.waitForTransaction({ digest: result.FailedTransaction.digest });
      failures[label] = { digest: result.FailedTransaction.digest, status: result.FailedTransaction.status.error?.message ?? status };
    }
    const code = (n, fn) => new RegExp(`abort code: ${n}, in '[^']*::${fn}'`);
    const shared = async (tx, id, mutable) => {
      const object = (await client.getObject({ objectId: id })).object;
      return tx.sharedObjectRef({ objectId: id, initialSharedVersion: object.owner.Shared.initialSharedVersion, mutable });
    };
    const owned = async (tx, id) => {
      const object = (await client.getObject({ objectId: id })).object;
      return tx.objectRef({ objectId: id, version: object.version, digest: object.digest });
    };

    const current = path.join(tmp, "current"); await fs.mkdir(current);
    await fs.cp(path.join(root, "contracts/sources"), path.join(current, "sources"), { recursive: true });
    for (const name of ["Move.toml", "Move.lock"]) await fs.copyFile(path.join(root, "contracts", name), path.join(current, name));
    let t = new Transaction(); const [cap] = t.publish(compile(path.join(oldTree, "contracts"))); t.transferObjects([cap], owner.toSuiAddress());
    const pub = await execute("publish pre-identity package", t);
    const old = pub.effects.changedObjects.find(o => o.outputState === "PackageWrite").objectId;
    const upgradeCap = created(pub, "::package::UpgradeCap"), registry = created(pub, "::device_registry::DeviceRegistry");
    t = new Transaction(); t.moveCall({target:`${old}::deployment_manager::create_deployment_verified`,arguments:[t.pure.string("legacy"),t.pure.string("1"),t.pure.string("blob"),t.pure.string("ab".repeat(32)),t.pure.string("cd".repeat(32)),t.pure.vector("id",[]),t.pure.vector("id",["0x10"]),t.object("0x6")]});
    const legacy = created(await execute("create on old package",t),"::deployment_manager::DeploymentRecord");
    const built = compile(current); t = new Transaction();
    const [ticket] = t.moveCall({target:"0x2::package::authorize_upgrade",arguments:[t.object(upgradeCap),t.pure.u8(0),t.pure.vector("u8",built.digest)]});
    const receipt=t.upgrade({modules:built.modules,dependencies:built.dependencies,package:old,ticket});
    t.moveCall({target:"0x2::package::commit_upgrade",arguments:[t.object(upgradeCap),receipt]});
    const head=(await execute("compatible pre-identity upgrade",t)).effects.changedObjects.find(o=>o.outputState==="PackageWrite").objectId;
    const pkg=bcs.Object.parse((await client.getObject({objectId:head,include:{objectBcs:true}})).object.objectBcs).data.Package;
    const origins=Object.fromEntries(pkg.typeOriginTable.map(o=>[`${o.moduleName}::${o.datatypeName}`,o.package]));
    const {fromBase58,toHex}=appRequire('@mysten/sui/utils');
    const chain=toHex(fromBase58((await client.ledgerService.getServiceInfo({})).response.chainId).slice(0,4));
    const manifest={schema_version:1,protocol_version:1,network:'localnet',chain_identifier:chain,rpc_url:rpc,call_package_id:head,upgrade_cap_id:upgradeCap,package_version:pkg.version,type_origins:origins,
      shared_objects:{registry:{id:registry,initial_shared_version:(await client.getObject({objectId:registry})).object.owner.Shared.initialSharedVersion}},
      supported_clients:{dapp:{min:'0.1.1',max_exclusive:'0.2.0'},edge:{min:'0.2.2',max_exclusive:'0.3.0'}},walrus:{publisher_url:'http://127.0.0.1:9',aggregator_url:'http://127.0.0.1:9'}};
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
    const load=loader(),app=load('lib/sui.ts'),shards=load('lib/receipt-shards.ts'),p=load('lib/fleet-protocol.ts');
    t=new Transaction();p.calls.deployment_manager_begin_sharded_receipts(t,head,legacy);await execute('protect oldest deletion path',t);
    await attack('old no-schema-check delete blocked',owner,async tx=>tx.moveCall({target:`${old}::deployment_manager::delete_deployment`,arguments:[await shared(tx,legacy,true)]}),/dynamic_field/);
    assert.equal((await app.readReceipts(client,legacy)).verified,true);
    await require('./receipt_test_helpers.cjs').deleteDeployment(client, tx=>execute('bounded legacy cleanup '+Object.keys(digests).length,tx),legacy);
    // Activate one device and use the real daemon/key and its normal report builder.
    await execute('activate probe device',app.buildRegisterDeviceTx('probe',owner.toSuiAddress(),'x86_64',owner.toSuiAddress()));
    const device=(await app.fetchDevices(client,owner.toSuiAddress()))[0];
    const caps=await client.listOwnedObjects({owner:owner.toSuiAddress(),type:`${origins['device_cap::DeviceCap']}::device_cap::DeviceCap`});
    const deviceCap=caps.objects[0].objectId;
    const {decodeSuiPrivateKey}=appRequire('@mysten/sui/cryptography');
    const key=path.join(tmp,'probe.key');await fs.writeFile(key,Buffer.from(decodeSuiPrivateKey(owner.getSecretKey()).secretKey).toString('hex'),{mode:0o600});
    await fs.writeFile(path.join(tmp,'deployment.json'),JSON.stringify(manifest));
    const state=path.join(tmp,'state'),logPath=path.join(tmp,'daemon.log');
    await fs.writeFile(path.join(tmp,'config.toml'),`[sui]\nmanifest_path = "deployment.json"\n[device]\nkeypair_path = ${JSON.stringify(key)}\ndevice_object_id = "${device.objectId}"\ndevice_cap_id = "${deviceCap}"\n[walrus]\ndownload_attempts = 1\ndownload_timeout_seconds = 2\n[apply]\ncommand = "/bin/false"\n[runtime]\nstate_dir = ${JSON.stringify(state)}\npoll_seconds = 1\n`);
    const log=await fs.open(logPath,'w');
    daemon=spawn(DAEMON,[],{env:{...process.env,EDGE_CONFIG:path.join(tmp,'config.toml'),NO_COLOR:'1'},stdio:['ignore',log.fd,log.fd]});
    const journal=async()=>{try{return JSON.parse(await fs.readFile(path.join(state,'state.json'),'utf8'));}catch{return{};}};
    async function waitFor(condition,label){for(let n=0;n<240;n++){if(await condition())return;if(daemon.exitCode!==null)throw Error(label+': daemon exited');await new Promise(r=>setTimeout(r,250));}throw Error(label+'\n'+JSON.stringify(await journal())+'\n'+(await fs.readFile(logPath,'utf8')).slice(-6000));}
    // initialized is saved before the discovery fence/backfill has completed.
    // Publish the probe only after the durable combined-discovery cursor exists.
    await waitFor(async()=>{const s=await journal();return s.initialized && s.discovery_cursor;},'daemon discovery baseline');
    const deployment=created(await execute('new sharded failure probe',app.buildCreateDeploymentTx('probe','1','missing-recipe','ab'.repeat(32),'cd'.repeat(32),[],[device.objectId])),'::deployment_manager::DeploymentRecord');
    await waitFor(async()=>(await journal()).deployments?.[deployment]?.reported,'real daemon v3 effects-checked report');
    await waitFor(async()=>{try{return (await app.readReceipts(client,deployment)).statuses[device.objectId]===2;}catch{return false;}},'checkpointed v3 receipt');
    assert.deepEqual((await app.readReceipts(client,deployment)).statuses,{[device.objectId]:2},JSON.stringify((await journal()).deployments[deployment]));
    // Schema-2 catalog shards must use relocated bindings and the v3 signer,
    // not fall back to old catalog resolution or the mutable v2 report path.
    t=new Transaction();t.moveCall({target:`${head}::component_catalog::create_catalog`});
    const catalog=created(await execute('create catalog client probe',t),'::component_catalog::ComponentCatalog');
    t=new Transaction();t.moveCall({target:`${head}::component_catalog::publish_version`,arguments:[t.object(catalog),t.pure.string('catalog.probe'),t.pure.string('1.0.0'),t.pure.vector('string',['linux']),t.pure.vector('string',['x86_64']),t.pure.vector('string',['missing-recipe']),t.pure.vector('string',['ab'.repeat(32)]),t.pure.vector('string',['cd'.repeat(32)]),t.object('0x6')]});
    const version=created(await execute('publish catalog client probe',t),'::component_catalog::ComponentVersion');
    t=new Transaction();t.moveCall({target:`${head}::component_catalog::create_deployment_paused`,arguments:[t.object(version),t.pure.vector('id',[]),t.pure.vector('id',[device.objectId]),t.object('0x6')]});
    const catalogDeployment=created(await execute('create paused catalog client probe',t),'::deployment_manager::DeploymentRecord');
    t=new Transaction();t.moveCall({target:`${head}::component_catalog::begin_sharded_receipts`,arguments:[t.object(catalogDeployment)]});await execute('shard catalog client probe',t);
    t=new Transaction();p.calls.deployment_manager_set_control(t,head,catalogDeployment,0,0,0,0,0,0);await execute('unpause catalog client probe',t);
    await waitFor(async()=>(await journal()).deployments?.[catalogDeployment]?.reported,'real daemon schema-2 catalog v3 report');
    await waitFor(async()=>{try{return (await app.readReceipts(client,catalogDeployment)).statuses[device.objectId]===2;}catch{return false;}},'checkpointed catalog v3 receipt');
    const catalogOutcome=(await journal()).deployments[catalogDeployment];
    assert.match(catalogOutcome.error,/http|recipe mirror|Walrus/i,'catalog authority must resolve before the deliberate missing-recipe failure');
    assert.equal((await app.fetchDeploymentEvents(client,owner.toSuiAddress())).find(d=>d.deploymentId===catalogDeployment).schemaVersion,'2');
    // The current main health path must resolve schema 4 through the directory.
    // A v3 receipt settles before the device's durable health retry can succeed.
    const rollout = load('lib/rollout.ts');
    const rolloutDeployment = created(await execute('create sharded rollout health probe', rollout.buildRolloutTx({
      component:'rollout.probe',version:'1',recipe:'missing-recipe',artifactSha256:'ab'.repeat(32),recipeSha256:'cd'.repeat(32),
      cohorts:[[device.objectId]],concurrency:1,rateCount:1,rateWindowMs:1000,bakeMs:0,minimumSamples:1,failureBps:10000,offlineTimeoutMs:3600000
    })), '::deployment_manager::DeploymentRecord');
    await execute('admit sharded rollout health probe', await rollout.buildAdvanceRolloutTx(client,rolloutDeployment,owner.toSuiAddress(),[device.objectId]));
    await waitFor(async()=>(await journal()).deployments?.[rolloutDeployment]?.reported,'real daemon sharded rollout report');
    await waitFor(async()=>{try{return (await app.readReceipts(client,rolloutDeployment)).statuses[device.objectId]===2;}catch{return false;}},'sharded rollout receipt checkpoint');
    await execute('settle sharded rollout receipt before health retry',await rollout.buildAdvanceRolloutTx(client,rolloutDeployment,owner.toSuiAddress()));
    let health, healthDigest;
    await waitFor(async()=>{try{
      const {dynamicField} = await client.getDynamicField({parentId:rolloutDeployment,name:{type:`${origins['deployment_manager::RolloutHealthKey']}::deployment_manager::RolloutHealthKey`,bcs:new Uint8Array([0])}});
      health=p.deployment_manager_RolloutHealthBcs.parse(dynamicField.value.bcs);
      healthDigest=dynamicField.previousTransaction;
      return health.devices.length===1;
    }catch{return false;}},'actual device-signed health on schema-4 sharded rollout');
    assert.ok(healthDigest);
    const healthExecution=await client.getTransaction({digest:healthDigest,include:{effects:true}});
    assert.equal(healthExecution.Transaction?.status.success,true);
    digests['independently verified device rollout health']=healthDigest;
    assert.deepEqual(health.devices,[device.objectId]);assert.deepEqual(health.healthy,[false]);assert.deepEqual(health.signers,[owner.toSuiAddress()]);
    await waitFor(async()=>(await journal()).deployments?.[rolloutDeployment]?.rollout_health_pending===false,'durable sharded health completion');
    await require('./receipt_test_helpers.cjs').deleteDeployment(client,tx=>execute('bounded rollout probe cleanup '+Object.keys(digests).length,tx),rolloutDeployment,'3');
    daemon.kill('SIGINT');await new Promise(resolve=>daemon.once('exit',resolve));daemon=undefined;await log.close();
    // An independent production indexer reads the exact same root/field layout.
    const indexer=path.join(root,'suifleet-indexer/target/debug/suifleet-indexer'),db=path.join(tmp,'fleet.db');
    execFileSync(indexer,['index','--manifest',path.join(tmp,'deployment.json'),'--db',db],{encoding:'utf8',timeout:120000,stdio:['ignore','pipe','pipe']});
    const indexed=JSON.parse(execFileSync('python3',['-c','import sqlite3,json,sys; print(json.dumps(sqlite3.connect(sys.argv[1]).execute("SELECT device_id,status FROM receipts WHERE deployment_id=?",(sys.argv[2],)).fetchall()))',db,deployment],{encoding:'utf8'}));
    assert.deepEqual(indexed,[[device.objectId,2]]);
    const catalogIndexed=JSON.parse(execFileSync('python3',['-c','import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps([c.execute("SELECT device_id,status FROM receipts WHERE deployment_id=?",(sys.argv[2],)).fetchall(),c.execute("SELECT schema_version,catalog_version FROM deployments WHERE id=?",(sys.argv[2],)).fetchone()]))',db,catalogDeployment],{encoding:'utf8'}));
    assert.deepEqual(catalogIndexed,[[[device.objectId,2]],[2,version]]);
    await require('./receipt_test_helpers.cjs').deleteDeployment(client,tx=>execute('bounded catalog probe cleanup '+Object.keys(digests).length,tx),catalogDeployment,'2');

    await require('./receipt_test_helpers.cjs').deleteDeployment(client,tx=>execute('bounded probe cleanup '+Object.keys(digests).length,tx),deployment);
    const evidence={result:'PASS',network:'isolated network-none localnet',base,old,head,digests,failures,daemon:'actual daemon built with Rust 1.94.0; missing-recipe Failed receipt through immutable-deployment v3 signing/transport/effects path for ordinary and schema-2 catalog records',indexer:'actual production index command independently materialized ordinary and schema-2 catalog v3 Failed receipts and the relocated catalog binding',rollout_probe:{deployment:rolloutDeployment,health},catalog_probe:{deployment:catalogDeployment,version,error:catalogOutcome.error},limits:['No successful artifact installation in this probe; existing OTA simulator covers successful apply','No public-network, hardware, mainnet or manual wallet acceptance']};
    await fs.writeFile(path.join(root,'docs/validation/sf127-clients-localnet.json'),JSON.stringify(evidence,null,2)+'\n');
    console.log('PASS: pre-identity old-package deletion rejected, actual daemon ordinary/catalog v3 reports, schema-4 rollout health retry, independent indexer receipt/binding read and bounded cleanup');
  } finally {if(daemon){daemon.kill('SIGKILL');await new Promise(resolve=>daemon.once('exit',resolve));}await fs.rm(tmp,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
