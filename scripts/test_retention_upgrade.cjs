// Existing v1 FleetStorage upgraded to v2; actual old-package calls must fail.
// Runs only under scripts/test_protocol_localnet.py in network-none Docker.
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { requestSuiFromFaucetV2 } = appRequire('@mysten/sui/faucet');
const { bcs } = appRequire('@mysten/sui/bcs');
const root = path.resolve(__dirname, '..');
(async () => {
  assert.deepEqual((await fs.readdir('/sys/class/net')).sort(), ['lo']);
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'sf092-upgrade-'));
  try {
    const client = new SuiGrpcClient({ network: 'localnet', baseUrl: 'http://127.0.0.1:9000' });
    let chain;
    const signer = Ed25519Keypair.generate();
    for (let attempt = 0; attempt < 30; attempt++) {
      try { await requestSuiFromFaucetV2({ host: 'http://127.0.0.1:9123', recipient: signer.toSuiAddress() }); break; }
      catch (error) { if (attempt === 29) throw error; await new Promise((resolve) => setTimeout(resolve, 1000)); }
    }
    for (let attempt = 0; attempt < 30; attempt++) {
      try { chain = (await client.getChainIdentifier()).chainIdentifier; break; }
      catch (error) { if (attempt === 29) throw error; await new Promise((resolve) => setTimeout(resolve, 1000)); }
    }
    const env = { ...process.env, SUI_CONFIG_DIR: path.join(tmp, 'wallet') };
    await fs.mkdir(env.SUI_CONFIG_DIR);
    await fs.writeFile(path.join(env.SUI_CONFIG_DIR, 'sui.keystore'), '[]');
    await fs.writeFile(path.join(env.SUI_CONFIG_DIR, 'client.yaml'), `keystore:\n  File: ${env.SUI_CONFIG_DIR}/sui.keystore\nenvs:\n- alias: testnet\n  rpc: http://127.0.0.1:9000\nactive_env: testnet\nactive_address: null\n`);
    async function copy(from, to) { await fs.cp(from, to, { recursive: true, filter: (name) => !['build', 'Published.toml'].includes(path.basename(name)) }); }
    const core = path.join(tmp, 'contracts'); await copy(path.join(root, 'contracts'), core);
    const cache = '/home/dev/.move/git/https___github_com_MystenLabs_walrus_git_c68b530e0b1e522b4a3896190d883e4312a1cf0e/testnet-contracts';
    await copy(path.join(cache, 'wal'), path.join(tmp, 'wal'));
    await copy(path.join(cache, 'walrus'), path.join(tmp, 'walrus'));
    // Resolve the same checksum-pinned cached framework sources by local path;
    // changed fixture manifests must not run git ls-remote in network-none tests.
    const framework = '/home/dev/.move/git/https___github_com_MystenLabs_sui_git_987123842623d6cb0e772dc665789cfd69bdec9f/crates/sui-framework/packages';
    for (const name of ['wal', 'walrus']) {
      const file = path.join(tmp, name, 'Move.toml');
      const text = (await fs.readFile(file, 'utf8')).replace(/\{ git = "https:\/\/github.com\/MystenLabs\/sui.git", subdir = "crates\/sui-framework\/packages\/([^"]+)", rev = "testnet-v1.82.1" \}/g,
        (_, directory) => `{ local = "${framework}/${directory}" }`);
      await fs.writeFile(file, text);
    }
    // Local fixture initialization only: real upstream System/Storage/Blob
    // layouts and mutation code, with a deterministic one-member committee.
    // Never publish this instrumented Walrus package outside network-none.
    const innerPath = path.join(tmp, 'walrus/sources/system/system_state_inner.move');
    await fs.appendFile(innerPath, `
public(package) fun retention_fixture(ctx: &mut TxContext): SystemStateInnerV1 {
    let mut state = create_empty(104, ctx);
    let member = bls_aggregate::new_bls_committee_member(
        sui::bls12381::g1_to_uncompressed_g1(&sui::bls12381::g1_generator()),
        100, object::id_from_address(@0xabc));
    state.committee = bls_aggregate::new_bls_committee(0, vector[member]);
    state.total_capacity_size = 1_000_000_000_000_000;
    state.storage_price_per_unit_size = 5;
    state.write_price_per_unit_size = 1;
    state
}
`);
    await fs.appendFile(path.join(tmp, 'walrus/sources/system.move'), `
public fun retention_fixture(ctx: &mut TxContext) {
    let mut system = System { id: object::new(ctx), version: VERSION,
        package_id: object::id_from_address(@0xabc), new_package_id: option::none() };
    dynamic_field::add(&mut system.id, VERSION, system_state_inner::retention_fixture(ctx));
    transfer::share_object(system);
}
`);
    if (process.env.SF128_SCALE_UPGRADE === '1') {
      // Isolated epoch control only; never publish instrumented Walrus publicly.
      await fs.appendFile(innerPath, `
public(package) fun sf128_set_epoch(state: &mut SystemStateInnerV1, epoch: u32) {
    let member = bls_aggregate::new_bls_committee_member(
        sui::bls12381::g1_to_uncompressed_g1(&sui::bls12381::g1_generator()),
        100, object::id_from_address(@0xabc));
    state.committee = bls_aggregate::new_bls_committee(epoch, vector[member]);
}
`);
      await fs.appendFile(path.join(tmp, 'walrus/sources/system.move'), `
public fun sf128_set_epoch(system: &mut System, epoch: u32) {
    system_state_inner::sf128_set_epoch(system.inner_mut(), epoch);
}
`);
    }
    const storage = path.join(core, 'fleet_storage');
    await fs.writeFile(path.join(storage, 'Move.toml'), (await fs.readFile(path.join(storage, 'Move.toml'), 'utf8'))
      .replace(/wal = \{[^\n]+\}/, `wal = { local = "${tmp}/wal" }`)
      .replace(/walrus = \{[^\n]+\}/, `walrus = { local = "${tmp}/walrus" }`));
    function compile(directory) {
      try {
        const text = execFileSync('sui', ['move', 'build', '--path', directory, '--build-env', 'testnet', '--no-tree-shaking', '--dump-bytecode-as-base64'], { env, encoding: 'utf8', maxBuffer: 30 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] });
        return JSON.parse(text.slice(text.indexOf('{')));
      } catch (error) { throw Error(`isolated compilation failed for ${path.basename(directory)}: ${String(error.stderr).slice(-4000)}`); }
    }
    const receipts = [];
    let latestGas;
    async function execute(label, tx) {
      if (process.env.SF128_SCALE_UPGRADE === '1') console.log(JSON.stringify({starting:label}));
      tx.setGasBudget(2000000000);
      if (latestGas) tx.setGasPayment([latestGas]);
      let result;
      for(let attempt=0;attempt<30;attempt++) {
        try {
          result=await client.signAndExecuteTransaction({transaction:tx,signer,include:{effects:true,objectTypes:true,events:true}});
          break;
        } catch(error) {
          // Only a pre-signing resolution failure is safe to retry. A submit
          // error or unknown outcome is never replaced.
          if(error.constructor.name!=='SimulationError' || !error.message.includes("provided version doesn't match") || attempt===29) throw error;
          await new Promise(resolve=>setTimeout(resolve,500));
        }
      }
      assert.ok(result.Transaction?.status.success, JSON.stringify(result.FailedTransaction?.status));
      const r = result.Transaction;
      await client.waitForTransaction({ digest: r.digest });
      const gas=r.effects.gasObject;
      if (gas?.outputVersion && gas?.outputDigest) latestGas={objectId:gas.objectId,version:gas.outputVersion,digest:gas.outputDigest};
      // Owned inputs must be visible at their effects version before the next
      // transaction resolves them. Receipt indexing alone is insufficient.
      for (const object of r.effects.changedObjects.filter(o=>o.outputOwner?.$kind === 'AddressOwner')) {
        for (let tries=0;tries<60;tries++) {
          const actual=(await client.getObject({objectId:object.objectId})).object;
          if(BigInt(actual.version)>=BigInt(object.outputVersion)) break;
          assert.ok(tries<59,'owned object effects not visible');
          await new Promise(resolve=>setTimeout(resolve,100));
        }
      }
      receipts.push({ label, digest: r.digest, gas: r.effects.gasUsed });
      if (process.env.SF128_SCALE_UPGRADE === '1') console.log(JSON.stringify({progress: label, digest:r.digest}));
      return r;
    }
    const created = (r, suffix) => Object.keys(r.objectTypes).find((key) => r.objectTypes[key].endsWith(suffix));
    async function publish(directory) {
      const tx = new Transaction();
      const [cap] = tx.publish(compile(directory)); tx.transferObjects([cap], signer.toSuiAddress());
      const r = await execute(`publish ${path.basename(directory)}`, tx);
      const pkg = r.effects.changedObjects.find((o) => o.outputState === 'PackageWrite').objectId;
      const upgrade = created(r, '::package::UpgradeCap');
      await fs.writeFile(path.join(directory, 'Published.toml'), `[published.testnet]\nchain-id = "${chain}"\npublished-at = "${pkg}"\noriginal-id = "${pkg}"\nversion = 1\ntoolchain-version = "1.81.1"\nbuild-config = { flavor = "sui", edition = "2024" }\nupgrade-capability = "${upgrade}"\n`);
      return { pkg, upgrade, result: r };
    }
    const walPub = await publish(path.join(tmp, 'wal'));
    const walrusPub = await publish(path.join(tmp, 'walrus'));
    let fixtureTx = new Transaction();
    fixtureTx.moveCall({ target: `${walrusPub.pkg}::system::retention_fixture` });
    const system = created(await execute('initialize isolated Walrus fixture', fixtureTx), '::system::System');
    const corePub = await publish(core);
    const registry = created(corePub.result, '::device_registry::DeviceRegistry');
    // Exact merged SF083 source, not a rewritten pseudo-v1.
    await fs.writeFile(path.join(storage, 'sources/fleet_storage.move'), execFileSync('git', ['show', '3e796f7c2a91e88734970b0d3956469e3a86c7ed:contracts/fleet_storage/sources/fleet_storage.move'], { cwd: root }));
    const base = await publish(storage);
    let tx = new Transaction();
    const [deposit] = tx.splitCoins(tx.gas, [tx.pure.u64(1000000)]);
    const [limits] = tx.moveCall({ target: `${corePub.pkg}::fleet_budget::new_limits`, arguments: [tx.pure.u64(10), tx.pure.u64(1000), tx.pure.u64(100), tx.pure.u64(10), tx.pure.u64(0)] });
    const [cap] = tx.moveCall({ target: `${corePub.pkg}::fleet_budget::create`, arguments: [tx.object(registry), deposit, limits, tx.pure.u64(1)] });
    tx.transferObjects([cap], signer.toSuiAddress());
    const budgetResult = await execute('create budget', tx);
    const budget = created(budgetResult, '::fleet_budget::FleetBudget'), ownerCap = created(budgetResult, '::fleet_budget::BudgetOwnerCap');
    tx = new Transaction();
    const [quota] = tx.moveCall({ target: `${base.pkg}::fleet_storage::new_quota`, arguments: [tx.pure.u64(1000), tx.pure.u64(100), tx.pure.u64(10), tx.pure.u64(10), tx.pure.u64(100)] });
    tx.moveCall({ target: `${base.pkg}::fleet_storage::create`, arguments: [tx.object(budget), tx.object(ownerCap), quota] });
    const oldStorage = created(await execute('create v1 storage', tx), '::fleet_storage::FleetStorage');
    tx = new Transaction();
    const walCoin = created(walPub.result, '::coin::Coin<' + walPub.pkg + '::wal::WAL>');
    const [walDeposit] = tx.splitCoins(tx.object(walCoin), [tx.pure.u64(1000000)]);
    tx.moveCall({ target: `${base.pkg}::fleet_storage::deposit_wal`, arguments: [tx.object(oldStorage), tx.object(budget), tx.object(ownerCap), walDeposit] });
    tx.moveCall({ target: `${base.pkg}::fleet_storage::buy_storage`, arguments: [tx.object(oldStorage), tx.object(budget), tx.object(ownerCap), tx.object(system), tx.pure.u64(10000000), tx.pure.u32(5)] });
    const [blobId] = tx.moveCall({ target: `${walrusPub.pkg}::blob::derive_blob_id`, arguments: [tx.pure.u256(123), tx.pure.u8(1), tx.pure.u64(10)] });
    tx.moveCall({ target: `${base.pkg}::fleet_storage::register_fleet_blob`, arguments: [tx.object(oldStorage), tx.object(budget), tx.object(ownerCap), tx.object(system), blobId, tx.pure.u256(123), tx.pure.u64(10), tx.pure.u8(1), tx.object('0x6')] });
    await execute('populate v1 pool and wrapped blob with real registration', tx);
    const before = (await client.getObject({ objectId: oldStorage, include: { content: true } })).object.content;
    const fieldsBefore = await client.listDynamicFields({ parentId: oldStorage, include: { value: true } });
    assert.ok(fieldsBefore.dynamicFields.some((f) => f.name.type.endsWith('::fleet_storage::RecordKey')));
    if (process.env.SF082_DEVICE_POLICY_UPGRADE === '1') await fs.writeFile(path.join(storage, 'sources/fleet_storage.move'), execFileSync('git',['show','2e9a7f4d1b35a9e69b6acc5da1a40ea39562e575:contracts/fleet_storage/sources/fleet_storage.move'],{cwd:root}));
    else if (process.env.SF128_SCALE_UPGRADE === '1') await fs.writeFile(path.join(storage, 'sources/fleet_storage.move'), execFileSync('git', ['show', 'e19970e045c42aad2452d5a67294fe1b770133f9:contracts/fleet_storage/sources/fleet_storage.move'], { cwd: root }));
    else await fs.copyFile(path.join(root, 'contracts/fleet_storage/sources/fleet_storage.move'), path.join(storage, 'sources/fleet_storage.move'));
    const built = compile(storage);
    tx = new Transaction();
    const [ticket] = tx.moveCall({ target: '0x2::package::authorize_upgrade', arguments: [tx.object(base.upgrade), tx.pure.u8(0), tx.pure.vector('u8', built.digest)] });
    const receipt = tx.upgrade({ modules: built.modules, dependencies: built.dependencies, package: base.pkg, ticket });
    tx.moveCall({ target: '0x2::package::commit_upgrade', arguments: [tx.object(base.upgrade), receipt] });
    const head = (await execute('compatible storage upgrade', tx)).effects.changedObjects.find((o) => o.outputState === 'PackageWrite').objectId;
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_storage::migrate`, arguments: [tx.object(oldStorage), tx.object(budget), tx.object(ownerCap)] });
    await execute('migrate existing storage', tx);
    const content = (await client.getObject({ objectId: oldStorage, include: { content: true } })).object.content;
    assert.equal(bcs.u64().parse(content.slice(32, 40)), process.env.SF082_DEVICE_POLICY_UPGRADE === '1' || process.env.SF128_SCALE_UPGRADE === '1' ? '2' : '3');
    assert.deepEqual(content.slice(40), before.slice(40), 'pool, WAL, counters and budget binding survive migration');
    const fieldsAfter = await client.listDynamicFields({ parentId: oldStorage, include: { value: true } });
    assert.deepEqual(fieldsAfter.dynamicFields, fieldsBefore.dynamicFields, 'wrapped blob and record origins/content survive');
    // Register through v2 on the existing v1-origin object and feed actual
    // confirmed events into the same parser used by browser certification.
    tx = new Transaction();
    const [newBlobId] = tx.moveCall({ target: `${walrusPub.pkg}::blob::derive_blob_id`, arguments: [tx.pure.u256(124), tx.pure.u8(1), tx.pure.u64(11)] });
    tx.moveCall({ target: `${head}::fleet_storage::register_fleet_blob`, arguments: [tx.object(oldStorage), tx.object(budget), tx.object(ownerCap), tx.object(system), newBlobId, tx.pure.u256(124), tx.pure.u64(11), tx.pure.u8(1), tx.object('0x6')] });
    const registered = await execute('v2 registration on migrated pool with v1-origin event', tx);
    const emitted = registered.events.find((event) => event.eventType.endsWith('::fleet_storage::FleetUploadRegistered'));
    assert.ok(emitted.eventType.startsWith(base.pkg + '::'));
    const upload = loader()('lib/fleet-upload.ts');
    const info = upload.registeredBlob(registered.events, { config: { packageId: head, storageOrigin: base.pkg }, storageId: oldStorage }, BigInt(bcs.u256().parse(emitted.bcs.slice(64, 96))));
    assert.equal(info.endEpoch, 5);
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_storage::configure_retention`, arguments: [tx.object(oldStorage), tx.object(budget), tx.object(ownerCap), tx.pure.address(signer.toSuiAddress()), tx.pure.u32(2), tx.pure.u64(100), tx.pure.u64(10), tx.pure.u64(1)] });
    await execute('configure on existing object', tx);
    // Resolve explicit refs to force an on-chain failure rather than dry-run refusal.
    const gas = (await client.listCoins({ owner: signer.toSuiAddress() })).objects[0];
    tx = new Transaction(); tx.setSender(signer.toSuiAddress()); tx.setGasBudget(50000000);
    tx.setGasPayment([{ objectId: gas.objectId, version: gas.version, digest: gas.digest }]);
    tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice);
    const shared = async (key) => {
      const object = (await client.getObject({ objectId: key })).object;
      return tx.sharedObjectRef({ objectId: key, initialSharedVersion: object.owner.Shared.initialSharedVersion, mutable: true });
    };
    const own = (await client.getObject({ objectId: ownerCap })).object;
    const [withdrawn] = tx.moveCall({ target: `${base.pkg}::fleet_storage::withdraw_wal`, arguments: [await shared(oldStorage), await shared(budget), tx.objectRef({ objectId: ownerCap, version: own.version, digest: own.digest }), tx.pure.u64(1)] });
    tx.transferObjects([withdrawn], signer.toSuiAddress());
    const bytes = await tx.build(), signed = await signer.signTransaction(bytes);
    const failed = await client.executeTransaction({ transaction: bytes, signatures: [signed.signature], include: { effects: true } });
    assert.equal(failed.$kind, 'FailedTransaction');
    assert.match(JSON.stringify(failed.FailedTransaction.status), /abort code: 0/);
    const failedGas=failed.FailedTransaction.effects.gasObject;
    latestGas={objectId:failedGas.objectId,version:failedGas.outputVersion,digest:failedGas.outputDigest};
    receipts.push({ label: 'old-package withdrawal rejected after migration', digest: failed.FailedTransaction.digest });
    if (process.env.SF082_DEVICE_POLICY_UPGRADE === '1') {
      const bytesBefore=(await client.getObject({objectId:oldStorage,include:{content:true}})).object.content;
      const priorFields=(await client.listDynamicFields({parentId:oldStorage,include:{value:true}})).dynamicFields;
      await fs.copyFile(path.join(root,'contracts/fleet_storage/sources/fleet_storage.move'),path.join(storage,'sources/fleet_storage.move'));
      const published=await fs.readFile(path.join(storage,'Published.toml'),'utf8');
      await fs.writeFile(path.join(storage,'Published.toml'),published.replace(`published-at = "${base.pkg}"`,`published-at = "${head}"`).replace('version = 1','version = 2'));
      const deviceBuilt=compile(storage);tx=new Transaction();
      const [t]=tx.moveCall({target:'0x2::package::authorize_upgrade',arguments:[tx.object(base.upgrade),tx.pure.u8(0),tx.pure.vector('u8',deviceBuilt.digest)]});
      const upgradeReceipt=tx.upgrade({modules:deviceBuilt.modules,dependencies:deviceBuilt.dependencies,package:head,ticket:t});
      tx.moveCall({target:'0x2::package::commit_upgrade',arguments:[tx.object(base.upgrade),upgradeReceipt]});
      const current=(await execute('upgrade exact merged SF092 to SF082 device policy',tx)).effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId;
      assert.deepEqual((await client.getObject({objectId:oldStorage,include:{content:true}})).object.content,bytesBefore);
      assert.deepEqual((await client.listDynamicFields({parentId:oldStorage,include:{value:true}})).dynamicFields,priorFields);
      const datatype=async name=>(await client.movePackageService.getDatatype({packageId:current,moduleName:'fleet_storage',name})).response.datatype.definingId;
      assert.equal(await datatype('FleetStorage'),base.pkg);assert.equal(await datatype('RetentionKey'),head);assert.equal(await datatype('DeviceRetentionKey'),current);
      tx=new Transaction();tx.moveCall({target:`${current}::fleet_storage::migrate`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap)]});await execute('migrate device-policy storage to current version',tx);
      tx=new Transaction();tx.moveCall({target:`${current}::fleet_storage::configure_device_retention`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.pure.u32(8),tx.pure.u64(100)]});
      await execute('owner configures later-origin device policy on existing v2 storage',tx);
      const actual=await loader()('lib/retention.ts').fetchDeviceRetention(client,{packageId:current,storageOrigin:base.pkg},oldStorage);
      assert.deepEqual(actual,{target:8,max_fee:'100'});
      tx=new Transaction();tx.moveCall({target:`${current}::fleet_storage::clear_device_retention`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap)]});await execute('owner clears device admission without deleting old history',tx);
      assert.equal(await loader()('lib/retention.ts').fetchDeviceRetention(client,{packageId:current,storageOrigin:base.pkg},oldStorage),undefined);
    }

    if (process.env.SF128_SCALE_UPGRADE === '1') {
      // Populate v2 renewal/prepaid/vector fields before the compatible upgrade.
      tx = new Transaction();
      const [funds] = tx.splitCoins(tx.object(walCoin), [tx.pure.u64(1000000)]);
      const [gasFunds] = tx.splitCoins(tx.gas, [tx.pure.u64(100)]);
      tx.moveCall({target: `${head}::fleet_storage::configure_retention`, arguments: [tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.pure.address(signer.toSuiAddress()),tx.pure.u32(2),tx.pure.u64(1000000),tx.pure.u64(100),tx.pure.u64(1)]});
      tx.moveCall({target: `${head}::fleet_storage::fund_retention`, arguments: [tx.object(oldStorage),funds,gasFunds]});
      const oldRecords=(await client.listDynamicFields({parentId:oldStorage,include:{value:true}})).dynamicFields.filter(f=>f.name.type.endsWith('::RecordKey'));
      for (const f of oldRecords) {
        const blob=bcs.Address.parse(f.name.bcs);
        tx.moveCall({target:`${head}::fleet_storage::authorize_renewal`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.pure.id(blob),tx.pure.u32(7),tx.pure.u64(100)]});
        tx.moveCall({target:`${head}::fleet_storage::prepay_renewal`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.object(system),tx.pure.id(blob)]});
      }
      await execute('populate v2 protected vector and real prepaid resources',tx);
      const prior=(await client.listDynamicFields({parentId:oldStorage,include:{value:true}})).dynamicFields;
      await fs.copyFile(path.join(root,'contracts/fleet_storage/sources/fleet_storage.move'),path.join(storage,'sources/fleet_storage.move'));
      const pub=await fs.readFile(path.join(storage,'Published.toml'),'utf8');
      await fs.writeFile(path.join(storage,'Published.toml'),pub.replace(`published-at = "${base.pkg}"`,`published-at = "${head}"`).replace('version = 1','version = 2'));
      const compiled=compile(storage);
      tx=new Transaction();
      const [authorization]=tx.moveCall({target:'0x2::package::authorize_upgrade',arguments:[tx.object(base.upgrade),tx.pure.u8(0),tx.pure.vector('u8',compiled.digest)]});
      const upgrade=tx.upgrade({modules:compiled.modules,dependencies:compiled.dependencies,package:head,ticket:authorization});
      tx.moveCall({target:'0x2::package::commit_upgrade',arguments:[tx.object(base.upgrade),upgrade]});
      const current=(await execute('compatible populated v2 to v3 upgrade',tx)).effects.changedObjects.find(o=>o.outputState==='PackageWrite').objectId;
      tx=new Transaction();tx.moveCall({target:`${current}::fleet_storage::migrate`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap)]});
      await execute('owner migration removes only legacy vector',tx);
      const after=(await client.listDynamicFields({parentId:oldStorage,include:{value:true}})).dynamicFields;
      const withoutVector=prior.filter(f=>!f.name.type.endsWith('::ProtectedBlobsKey'));
      assert.equal(after.length,withoutVector.length);
      const sorted=a=>a.sort((a,b)=>a.fieldId.localeCompare(b.fieldId));
      assert.deepEqual(sorted(after),sorted(withoutVector),'keyed renewal, prepaid storage, records and blobs retain original content and origins');
      assert.equal(bcs.u64().parse((await client.getObject({objectId:oldStorage,include:{content:true}})).object.content.slice(32,40)),'3');
      if (process.env.SF128_UPGRADE_ONLY !== '1') await require('./test_retention_scale.cjs')({client,signer,Transaction,bcs,execute,current,budget,ownerCap,system,walCoin,walrusPackage:walrusPub.pkg,created,receipts});
      tx=new Transaction();
      tx.moveCall({target:`${walrusPub.pkg}::system::sf128_set_epoch`,arguments:[tx.object(system),tx.pure.u32(6)]});
      tx.moveCall({target:`${current}::fleet_storage::refill_pool`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.object(system),tx.pure.u64(10000000),tx.pure.u32(8)]});
      const [freshId]=tx.moveCall({target:`${walrusPub.pkg}::blob::derive_blob_id`,arguments:[tx.pure.u256(99001),tx.pure.u8(1),tx.pure.u64(10)]});
      tx.moveCall({target:`${current}::fleet_storage::register_retained_fleet_blob`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.object(system),freshId,tx.pure.u256(99001),tx.pure.u64(10),tx.pure.u8(1),tx.object('0x6'),tx.pure.u32(9),tx.pure.u64(100)]});
      await execute('expired v2 protected blobs do not block new v3 retained registration',tx);
      async function abort(label, transaction, code) {
        transaction.setGasBudget(50000000);
        transaction.setSender(signer.toSuiAddress());
        transaction.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice);
        transaction.setGasPayment([latestGas]);
        transaction.addBuildPlugin(async (data, _options, next) => {
          for(let i=0;i<data.inputs.length;i++) {
            const input=data.inputs[i].UnresolvedObject;
            if(!input) continue;
            const object=(await client.getObject({objectId:input.objectId})).object;
            data.inputs[i]=object.owner.$kind==='Shared'
              ? {$kind:'Object',Object:{$kind:'SharedObject',SharedObject:{objectId:object.objectId,initialSharedVersion:object.owner.Shared.initialSharedVersion,mutable:object.objectId===oldStorage||object.objectId===system}}}
              : {$kind:'Object',Object:{$kind:'ImmOrOwnedObject',ImmOrOwnedObject:{objectId:object.objectId,version:object.version,digest:object.digest}}};
          }
          await next();
        });
        const result=await client.signAndExecuteTransaction({transaction,signer,include:{effects:true}});
        assert.equal(result.$kind,'FailedTransaction',label);
        assert.match(JSON.stringify(result.FailedTransaction.status),new RegExp('abort code: '+code+'(?:[^0-9]|$)'));
        const gas=result.FailedTransaction.effects.gasObject;
        latestGas={objectId:gas.objectId,version:gas.outputVersion,digest:gas.outputDigest};
        receipts.push({label,digest:result.FailedTransaction.digest});
      }
      tx=new Transaction();
      tx.moveCall({target:`${current}::fleet_storage::execute_renewal`,arguments:[tx.object(oldStorage),tx.object(system),tx.pure.id(bcs.Address.parse(oldRecords[0].name.bcs)),tx.pure.u32(5),tx.pure.u64(2)]});
      await abort('expired selected blob renewal still fails closed',tx,13);
      tx=new Transaction();
      const [w]=tx.moveCall({target:`${head}::fleet_storage::withdraw_wal`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.pure.u64(0)]});
      tx.transferObjects([w],signer.toSuiAddress());
      await abort('v2 mutation rejects migrated v3 storage',tx,0);
      // An owner-approved absolute deadline remains finite even when the
      // archive purchase itself would need no renewal.
      tx=new Transaction();
      const [device]=tx.moveCall({target:`${corePub.pkg}::device_registry::register_device`,arguments:[tx.object(registry),tx.pure.string('sf128'),tx.pure.address(signer.toSuiAddress()),tx.pure.string('x86_64'),tx.object('0x6')]});
      const [deviceCap]=tx.moveCall({target:`${corePub.pkg}::device_cap::issue`,arguments:[device]});
      const [deviceOwner]=tx.moveCall({target:`${corePub.pkg}::device_registry::activate_device`,arguments:[tx.object(registry),device]});
      tx.moveCall({target:`${corePub.pkg}::fleet_budget::enroll_device`,arguments:[tx.object(budget),tx.object(ownerCap),tx.object(registry),deviceOwner,tx.pure.u64(0),tx.pure.u64(0),tx.object('0x6')]});
      tx.transferObjects([deviceCap,deviceOwner],signer.toSuiAddress());
      const enrollment=await execute('enroll actual device for finite fully-paid admission regression',tx);
      const account=created(enrollment,'::fleet_budget::GasAccount'),cap=created(enrollment,'::device_cap::DeviceCap');
      tx=new Transaction();
      const [quota]=tx.moveCall({target:`${current}::fleet_storage::new_quota`,arguments:[tx.pure.u64(1000),tx.pure.u64(10000000),tx.pure.u64(10),tx.pure.u64(100),tx.pure.u64(1000)]});
      tx.moveCall({target:`${current}::fleet_storage::set_default_quota`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),quota]});
      tx.moveCall({target:`${current}::fleet_storage::configure_device_retention`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.pure.u32(5),tx.pure.u64(100)]});
      await execute('configure already-passed absolute device target at epoch six',tx);
      function deviceUpload(root) {
        const [id]=tx.moveCall({target:`${walrusPub.pkg}::blob::derive_blob_id`,arguments:[tx.pure.u256(root),tx.pure.u8(1),tx.pure.u64(10)]});
        tx.moveCall({target:`${current}::fleet_storage::register_blob`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(account),tx.object(registry),tx.object(cap),tx.object(system),id,tx.pure.u256(root),tx.pure.u64(10),tx.pure.u8(1),tx.object('0x6')]});
      }
      tx=new Transaction();deviceUpload(99002);
      await abort('expired absolute admission target rejects fully-paid archive',tx,13);
      tx=new Transaction();
      tx.moveCall({target:`${current}::fleet_storage::configure_device_retention`,arguments:[tx.object(oldStorage),tx.object(budget),tx.object(ownerCap),tx.pure.u32(8),tx.pure.u64(100)]});
      await execute('approve live target already covered by initial pool purchase',tx);
      tx=new Transaction();deviceUpload(99003);
      await execute('actual device fully-paid archive admission without renewal',tx);
      const paidFields=(await client.listDynamicFields({parentId:oldStorage,include:{value:true}})).dynamicFields;
      assert.ok(paidFields.some(f=>f.name.type===current+'::fleet_storage::FullyPaidArchiveKey'));


    }
    console.log(JSON.stringify({ check: 'populated-object compatible storage upgrade and old-package retirement', receipts }));
  } finally { await fs.rm(tmp, { recursive: true, force: true }); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
