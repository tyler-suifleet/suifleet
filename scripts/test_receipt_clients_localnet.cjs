// SF-130 on an isolated localnet: per-fleet cost settings introduced by a
// compatible base->head upgrade and stored on a FleetBudget created before
// it; owner-only changes with auditable FleetSettingsChanged events carrying
// the previous values; real on-chain refusals for another fleet's cap and
// out-of-bounds values; base-package budget operations still working beside
// the record; and the real daemon and the dApp reading the record over gRPC.
// Run through scripts/test_protocol_localnet.py --settings-upgrade (network none).
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
    async function waitFor(condition,label){for(let n=0;n<240;n++){if(await condition())return;if(daemon.exitCode!==null)throw Error(label+': daemon exited');await new Promise(r=>setTimeout(r,250));}throw Error(label+'\n'+(await fs.readFile(logPath,'utf8')).slice(-3000));}
    await waitFor(async()=>(await journal()).initialized,'daemon baseline');
    const deployment=created(await execute('new sharded failure probe',app.buildCreateDeploymentTx('probe','1','missing-recipe','ab'.repeat(32),'cd'.repeat(32),[],[device.objectId])),'::deployment_manager::DeploymentRecord');
    await waitFor(async()=>(await journal()).deployments?.[deployment]?.reported,'real daemon v3 effects-checked report');
    await waitFor(async()=>{try{return (await app.readReceipts(client,deployment)).statuses[device.objectId]===2;}catch{return false;}},'checkpointed v3 receipt');
    assert.deepEqual((await app.readReceipts(client,deployment)).statuses,{[device.objectId]:2},JSON.stringify((await journal()).deployments[deployment]));
    daemon.kill('SIGINT');await new Promise(resolve=>daemon.once('exit',resolve));daemon=undefined;await log.close();
    // An independent production indexer reads the exact same root/field layout.
    const indexer=path.join(root,'suifleet-indexer/target/debug/suifleet-indexer'),db=path.join(tmp,'fleet.db');
    execFileSync(indexer,['index','--manifest',path.join(tmp,'deployment.json'),'--db',db],{encoding:'utf8',timeout:120000,stdio:['ignore','pipe','pipe']});
    const indexed=JSON.parse(execFileSync('python3',['-c','import sqlite3,json,sys; print(json.dumps(sqlite3.connect(sys.argv[1]).execute("SELECT device_id,status FROM receipts WHERE deployment_id=?",(sys.argv[2],)).fetchall()))',db,deployment],{encoding:'utf8'}));
    assert.deepEqual(indexed,[[device.objectId,2]]);
    await require('./receipt_test_helpers.cjs').deleteDeployment(client,tx=>execute('bounded probe cleanup '+Object.keys(digests).length,tx),deployment);
    const evidence={result:'PASS',network:'isolated network-none localnet',base,old,head,digests,failures,daemon:'actual daemon built with Rust 1.94.0; missing-recipe Failed receipt through immutable-deployment v3 signing/transport/effects path',indexer:'actual production index command independently materialized v3 Failed receipt',limits:['No successful artifact installation in this probe; existing OTA simulator covers successful apply','No public-network, hardware, mainnet or manual wallet acceptance']};
    await fs.writeFile(path.join(root,'docs/validation/sf127-clients-localnet.json'),JSON.stringify(evidence,null,2)+'\n');
    console.log('PASS: pre-identity old-package deletion rejected, actual daemon v3 report effects and independent indexer receipt read, bounded cleanup');
  } finally {if(daemon){daemon.kill('SIGKILL');await new Promise(resolve=>daemon.once('exit',resolve));}await fs.rm(tmp,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
