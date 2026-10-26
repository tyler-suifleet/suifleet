// SF-127: existing-object receipt migration, real old-package attacks (including
// catalog handover), 1000-target storage measurement and bounded cleanup.
// Run through scripts/test_protocol_localnet.py --receipt-shards (network none).
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
    const base = execFileSync("git", ["merge-base", "HEAD", "origin/main"], { encoding: "utf8" }).trim();
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
    for (const signer of [owner, owner, stranger]) {
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

    const createdAll = (r, suffix) => Object.keys(r.objectTypes).filter(id => r.objectTypes[id].endsWith(suffix));
    let tx = new Transaction(); const [upgradeCapResult] = tx.publish(compile(path.join(oldTree, "contracts"))); tx.transferObjects([upgradeCapResult], owner.toSuiAddress());
    const published = await execute("publish base", tx);
    const old = published.effects.changedObjects.find(o => o.outputState === "PackageWrite").objectId;
    const upgradeCap = created(published, "::package::UpgradeCap"), registry = created(published, "::device_registry::DeviceRegistry");
    const devices = [], caps = [];
    const signers = [owner]; // all 1000 logical devices use this disposable key
    for (let start = 0; start < 1000; start += 100) {
      tx = new Transaction(); const owners = [];
      for (let n = start; n < start + 100; n++) {
        const [device] = tx.moveCall({ target: `${old}::device_registry::register_device`, arguments: [tx.object(registry), tx.pure.string(`r-${n}`), tx.pure.address(owner.toSuiAddress()), tx.pure.string("aarch64"), tx.object("0x6")] });
        tx.moveCall({ target: `${old}::device_cap::issue_cap`, arguments: [device] });
        const [cap] = tx.moveCall({ target: `${old}::device_registry::activate_device`, arguments: [tx.object(registry), device] }); owners.push(cap);
      }
      tx.transferObjects(owners, owner.toSuiAddress()); const r = await execute(`register ${start}`, tx);
      const capIds = createdAll(r, "::device_cap::DeviceCap");
      for (let offset = 0; offset < capIds.length; offset += 50) {
       const { objects } = await client.getObjects({ objectIds: capIds.slice(offset, offset + 50), include: { content: true } });
       for (const object of objects) { const id = object.objectId;
        const wire = bcs.struct("DeviceCap", { id: bcs.Address, device_id: bcs.Address }).parse(object.content);
        devices.push(wire.device_id); caps.push(id);
       }
      }
    }
    assert.equal(devices.length, 1000);
    tx = new Transaction();
    const targetVector = tx.makeMoveVec({ type: "0x2::object::ID", elements: devices.slice(0, 500).map(d => tx.pure.id(d)) });
    const targetTail = tx.makeMoveVec({ type: "0x2::object::ID", elements: devices.slice(500).map(d => tx.pure.id(d)) });
    tx.moveCall({ target: '0x1::vector::append', typeArguments: ['0x2::object::ID'], arguments: [targetVector, targetTail] });
    tx.moveCall({ target: `${old}::deployment_manager::create_deployment_verified`, arguments: [tx.pure.string("receipts"), tx.pure.string("1"), tx.pure.string("recipe"), tx.pure.string("aa".repeat(32)), tx.pure.string("bb".repeat(32)), tx.pure.vector("id", []), targetVector, tx.object("0x6")] });
    const deployment = created(await execute("create pre-upgrade deployment", tx), "::deployment_manager::DeploymentRecord");
    for (let start = 0; start < 70; start += 10) {
      tx = new Transaction();
      for (let n = start; n < start + 10; n++) tx.moveCall({ target: `${old}::deployment_manager::report_verified_status`, arguments: [tx.object(deployment), tx.object(registry), tx.object(caps[n]), tx.pure.u8(1)] });
      await execute(`seed v2 ${start}`, tx);
    }
    // Create catalog authority and confirmed handover under the actual base.
    tx = new Transaction(); tx.moveCall({target:`${old}::component_catalog::create_catalog`});
    const catalog = created(await execute("create base catalog", tx), "::component_catalog::ComponentCatalog");
    tx = new Transaction(); tx.moveCall({target:`${old}::component_catalog::publish_version`, arguments:[tx.object(catalog), tx.pure.string("catalog.app"), tx.pure.string("1.0.0"), tx.pure.vector("string",["linux"]), tx.pure.vector("string",["aarch64"]), tx.pure.vector("string",["recipe"]), tx.pure.vector("string",["aa".repeat(32)]), tx.pure.vector("string",["bb".repeat(32)]), tx.object("0x6")]});
    const version = created(await execute("publish base catalog version", tx), "::component_catalog::ComponentVersion");
    tx = new Transaction(); tx.moveCall({target:`${old}::component_catalog::create_deployment`, arguments:[tx.object(version), tx.pure.vector("id",[]), tx.pure.vector("id",[devices[0]]), tx.object("0x6")]});
    const catalogDeployment = created(await execute("create base catalog deployment", tx), "::deployment_manager::DeploymentRecord");
    tx = new Transaction(); tx.moveCall({target:`${old}::deployment_manager::report_verified_status`,arguments:[tx.object(catalogDeployment),tx.object(registry),tx.object(caps[0]),tx.pure.u8(1)]});await execute("seed base catalog receipt",tx);
    tx = new Transaction(); tx.moveCall({target:`${old}::fleet_handover::propose`, arguments:[tx.pure.address(stranger.toSuiAddress()),tx.pure.vector("id",[catalogDeployment]),tx.pure.vector("u64",[2]),tx.pure.u64(Date.now()+3600000),tx.object("0x6")]});
    const handover = created(await execute("propose base catalog handover",tx),"::fleet_handover::Handover");
    tx = new Transaction();tx.moveCall({target:`${old}::fleet_handover::confirm`,arguments:[tx.object(handover),tx.object("0x6")]});await execute("confirm base catalog handover",tx,stranger);
    const oldHandover = async t => t.moveCall({target:`${old}::fleet_handover::deployment`,arguments:[await shared(t,handover,true),await shared(t,catalogDeployment,true),await shared(t,"0x6",false)]});
    await attack("unsharded catalog rejects old handover",owner,oldHandover,code(5,"handover"));
    const tree = path.join(tmp, "head"); await fs.mkdir(tree);
    await fs.cp(path.join(root, "contracts/sources"), path.join(tree, "sources"), { recursive: true });
    for (const name of ["Move.toml", "Move.lock"]) await fs.copyFile(path.join(root, "contracts", name), path.join(tree, name));
    const built = compile(tree); tx = new Transaction();
    // Match the reviewed PR279 reservation for the enlarged current-main package.
    tx.setGasBudget(700_000_000);
    const [ticket] = tx.moveCall({ target: "0x2::package::authorize_upgrade", arguments: [tx.object(upgradeCap), tx.pure.u8(0), tx.pure.vector("u8", built.digest)] });
    const receipt = tx.upgrade({ modules: built.modules, dependencies: built.dependencies, package: old, ticket });
    tx.moveCall({ target: "0x2::package::commit_upgrade", arguments: [tx.object(upgradeCap), receipt] });
    const head = (await execute("compatible base to head", tx)).effects.changedObjects.find(o => o.outputState === "PackageWrite").objectId;
    const pkg = bcs.Object.parse((await client.getObject({ objectId: head, include: { objectBcs: true } })).object.objectBcs).data.Package;
    const origins = Object.fromEntries(pkg.typeOriginTable.map(o => [`${o.moduleName}::${o.datatypeName}`, o.package]));
    assert.equal(origins['deployment_manager::Receipts'], old);
    assert.equal(origins['deployment_manager::ReceiptShard'], head);
    const manifest = {
      schema_version: 1, protocol_version: 1, network: "localnet", chain_identifier: appRequire("@mysten/sui/utils").toHex(appRequire("@mysten/sui/utils").fromBase58((await client.ledgerService.getServiceInfo({})).response.chainId).slice(0,4)), rpc_url: rpc,
      call_package_id: head, upgrade_cap_id: upgradeCap, package_version: pkg.version, type_origins: origins,
      shared_objects: { registry: { id: registry, initial_shared_version: (await client.getObject({ objectId: registry })).object.owner.Shared.initialSharedVersion } },
      supported_clients: { dapp: { min: "0.1.0", max_exclusive: "0.2.0" }, edge: { min: "0.2.1", max_exclusive: "0.3.0" } },
      walrus: { publisher_url: "http://localhost:31415", aggregator_url: "http://localhost:31416" }
    };
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
    const load = loader(), app = load("lib/sui.ts"), shards = load("lib/receipt-shards.ts");
    const protocol = load("lib/fleet-protocol.ts");
    const call = (t, fn, args) => t.moveCall({ target: `${head}::deployment_manager::${fn}`, arguments: args });
    tx = new Transaction(); tx.moveCall({target:`${head}::component_catalog::begin_sharded_receipts`,arguments:[tx.object(catalogDeployment)]});await execute("shard catalog with inline schema 2",tx);
    await attack("sharded catalog rejects old handover",owner,oldHandover,code(5,"handover"));
    await attack("sharded catalog rejects old catalog delete",owner,async t => t.moveCall({target:`${old}::component_catalog::delete_deployment`,arguments:[await shared(t,catalogDeployment,true)]}),/dynamic_field/);
    await attack("sharded catalog rejects old ordinary delete",owner,async t => t.moveCall({target:`${old}::deployment_manager::delete_deployment`,arguments:[await shared(t,catalogDeployment,true)]}),code(5,"delete_as"));
    await attack("sharded catalog rejects old reports",owner,async t => t.moveCall({target:`${old}::deployment_manager::report_verified_status`,arguments:[await shared(t,catalogDeployment,true),await shared(t,registry,false),await owned(t,caps[0]),t.pure.u8(1)]}),code(5,"receipts_mut"));
    const catalogDirectory = await shards.readReceiptDirectory(client,catalogDeployment);
    assert.equal(catalogDirectory.original_schema,"2");assert.equal(catalogDirectory.ready,false);
    assert.equal((await app.readReceipts(client,catalogDeployment)).verified,false);
    tx = new Transaction();for (const root of catalogDirectory.roots) call(tx,"migrate_receipt_chunk",[tx.object(catalogDeployment),tx.object(root),tx.pure.u64(64)]);
    call(tx,"finish_receipt_migration",[tx.object(catalogDeployment)]);await execute("finish populated catalog migration",tx);
    await attack("ready catalog rejects old handover",owner,oldHandover,code(5,"handover"));
    const listed = (await app.fetchDeploymentEvents(client,owner.toSuiAddress())).find(d=>d.deploymentId===catalogDeployment);
    assert.equal(listed.schemaVersion,"2");assert.deepEqual(listed.targetDevices,[devices[0]]);assert.deepEqual(listed.targetGroups,[]);assert.equal(listed.receiptsVerified,true);
    tx = new Transaction();call(tx,"report_status_v3",[tx.object(catalogDeployment),tx.object(catalogDirectory.roots[shards.receiptShardIndex(devices[0])]),tx.object(registry),tx.object(caps[0]),tx.pure.u8(1)]);await execute("catalog v3 report",tx);
    assert.equal((await app.readReceipts(client,catalogDeployment)).statuses[devices[0]],1);
    tx = new Transaction();call(tx,"begin_receipt_cleanup",[tx.object(catalogDeployment)]);await execute("retire catalog barrier",tx);
    await attack("retiring catalog rejects old handover",owner,oldHandover,code(5,"handover"));
    tx = new Transaction();for (const root of catalogDirectory.roots) {call(tx,"cleanup_receipt_chunk",[tx.object(catalogDeployment),tx.object(root),tx.pure.u64(64)]);call(tx,"retire_receipt_shard",[tx.object(catalogDeployment),tx.object(root)]);}
    tx.moveCall({target:`${head}::component_catalog::delete_deployment`,arguments:[tx.object(catalogDeployment)]});await execute("delete catalog after bounded retirement",tx);
    tx = new Transaction(); call(tx, "begin_sharded_receipts", [tx.object(deployment)]); await execute("begin v3 barrier", tx);
    const directory = await shards.readReceiptDirectory(client, deployment), roots = directory.roots;
    assert.equal(roots.length, 16); assert.equal(directory.ready, false);
    await attack("old package report blocked", owner, async t => t.moveCall({ target: `${old}::deployment_manager::report_verified_status`, arguments: [await shared(t, deployment, true), await shared(t, registry, false), await owned(t, caps[0]), t.pure.u8(2)] }), code(5, "receipts_mut"));
    await attack("old delete blocked", owner, async t => t.moveCall({ target: `${old}::deployment_manager::delete_deployment`, arguments: [await shared(t, deployment, true)] }), code(5, "delete_as"));
    await attack("legacy read rejects v3", owner, async t => call(t, "receipt_status", [await shared(t, deployment, false), t.pure.id(devices[0])]), code(5, "receipt_status"));
    assert.equal((await app.readReceipts(client, deployment)).verified, false);
    tx = new Transaction();
    for (const root of roots) call(tx, "migrate_receipt_chunk", [tx.object(deployment), tx.object(root), tx.pure.u64(64)]);
    await execute("migration first bounded chunk", tx);
    await attack("partial migration cannot finish", owner, async t => call(t, "finish_receipt_migration", [await shared(t, deployment, true)]), code(15, "finish_receipt_migration"));
    tx = new Transaction();
    for (const root of roots) call(tx, "migrate_receipt_chunk", [tx.object(deployment), tx.object(root), tx.pure.u64(64)]);
    call(tx, "finish_receipt_migration", [tx.object(deployment)]); await execute("migration resume and finish", tx);
    assert.equal(Object.keys((await app.readReceipts(client, deployment)).statuses).length, 70);
    tx = new Transaction(); call(tx, "begin_sharded_receipts", [tx.object(deployment)]); call(tx, "finish_receipt_migration", [tx.object(deployment)]); await execute("idempotent migration retry", tx);
    await attack("wrong shard rejected", owner, async t => call(t, "report_status_v3", [await shared(t, deployment, false), await shared(t, roots[(shards.receiptShardIndex(devices[0]) + 1) % 16], true), await shared(t, registry, false), await owned(t, caps[0]), t.pure.u8(1)]), code(14, "record_sharded"));
    // Independently signed, effects-checked PTBs with a float-sized gas budget.
    // First and last are single-receipt writes; intervening batches contain at most 5.
    const gas = [];
    for (let start = 70; start < 1000;) {
      const count = start === 70 || start === 999 ? 1 : Math.min(5, 999 - start);
      tx = new Transaction(); tx.setGasBudget(60_000_000);
      for (let n = start; n < start + count; n++) call(tx, "report_status_v3", [tx.object(deployment), tx.object(roots[shards.receiptShardIndex(devices[n])]), tx.object(registry), tx.object(caps[n]), tx.pure.u8(1)]);
      const r = await execute(`v3 reports ${start}`, tx);
      const cost = r.effects.gasUsed;
      gas.push({ start, count, digest: r.digest, computation: cost.computationCost, storage: cost.storageCost, rebate: cost.storageRebate, gross: String(BigInt(cost.computationCost) + BigInt(cost.storageCost)) });
      assert.ok((BigInt(cost.computationCost) + BigInt(cost.storageCost)) / BigInt(count) < 60_000_000n, JSON.stringify({ start, count, cost }));
      if (start === 70 || start + count >= 999) console.log(`MEASURE: ${start} count ${count} gross ${BigInt(cost.computationCost) + BigInt(cost.storageCost)} MIST`);
      start += count;
    }
    const statuses = await app.readReceipts(client, deployment);
    assert.equal(statuses.verified, true); assert.equal(Object.keys(statuses.statuses).length, 1000);
    assert.ok(Object.values(statuses.statuses).every(s => s === 1));
    // The previous browser's direct final delete must fail on the real chain.
    await attack("old browser skips retirement", owner, async t => call(t, "delete_deployment", [await shared(t, deployment, true)]), code(17, "finish_receipt_cleanup"));
    await require('./receipt_test_helpers.cjs').deleteDeployment(client, t => execute('browser phased cleanup ' + Object.keys(digests).length, t), deployment);
    // Legacy unsharded deployments still take the final-delete path directly.
    tx = new Transaction(); call(tx, "create_deployment_verified", [tx.pure.string('legacy cleanup'), tx.pure.string('1'), tx.pure.string('recipe'), tx.pure.string('aa'.repeat(32)), tx.pure.string('bb'.repeat(32)), tx.pure.vector('id', []), tx.pure.vector('id', [devices[0]]), tx.object('0x6')]);
    const legacyCleanup = created(await execute('unsharded browser cleanup fixture', tx), '::deployment_manager::DeploymentRecord');
    assert.equal(await shards.readReceiptDirectory(client, legacyCleanup), undefined);
    await require('./receipt_test_helpers.cjs').deleteDeployment(client, t => execute('browser legacy cleanup', t), legacyCleanup);
    for (const root of roots) { const r = await client.getObjects({ objectIds: [root] }); assert.ok(r.objects[0] instanceof Error); }
    const evidence = { result: "PASS", network: "isolated network-none localnet", base, old, head, deployment, targets: 1000, receipt_shards: 16, preserved_v2_receipts: 70, receipt_transactions: gas, digests, failures, max_single_receipt_gross: String(gas.filter(g => g.count === 1).reduce((m, g) => BigInt(g.gross) > m ? BigInt(g.gross) : m, 0n)), float: 60000000, limits: ["No public-network, wallet extension, hardware or mainnet evidence", "120000 capacity follows fixed root and per-entry storage; this run populates 1000"] };
    await fs.mkdir(path.join(root, "docs/validation"), { recursive: true });
    await fs.writeFile(path.join(root, "docs/validation/sf127-localnet.json"), JSON.stringify(evidence, null, 2) + "\n");
    console.log(`PASS: compatible upgrade, barrier/old-package attacks, resumable migration, 1000 receipts and complete cleanup; max single receipt gross ${evidence.max_single_receipt_gross} MIST`);
  } finally { if (daemon) daemon.kill(); await fs.rm(tmp, { recursive: true, force: true }); }
})().catch(error => { console.error(error); process.exitCode = 1; });
