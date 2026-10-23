// SF-129 reclaim lifecycle on a disposable, network-none Sui chain.
// Real pinned Walrus/storage/core/data Move code; the only fixture code creates
// the one-member Walrus System and advances its epoch. Archives are certified
// with a real BLS certificate. Base objects are created before the in-place
// upgrade, then reclaimed through the dApp planner and executed effects.
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict'), { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc'), { Transaction } = appRequire('@mysten/sui/transactions'), { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { requestSuiFromFaucetV2 } = appRequire('@mysten/sui/faucet'), { bcs } = appRequire('@mysten/sui/bcs'), { fromBase58, toHex, normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
const BASE = process.env.SF129_BASE_REF || 'e19970e045c42aad2452d5a67294fe1b770133f9';
const STORAGE_EPOCHS = 3; // default retention: each new archive buys three epochs
const DEVICES = 4;

(async () => {
  assert.equal(process.env.PROTOCOL_TEST_DISPOSABLE, '1'); assert.deepEqual((await fs.readdir('/sys/class/net')).sort(), ['lo']);
  const root = path.resolve(__dirname, '..'), tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'sf129-reclaim-'));
  const { bls12_381 } = await import(path.join(root, 'dapp/node_modules/.pnpm/@noble+curves@2.4.0/node_modules/@noble/curves/bls12-381.js'));
  try {
    const rpc = process.env.PROTOCOL_TEST_RPC, client = new SuiGrpcClient({ network: 'localnet', baseUrl: rpc });
    let chainId;
    for (let attempt = 0; !chainId; attempt++) {
      try { chainId = (await client.getChainIdentifier()).chainIdentifier; }
      catch (error) { if (attempt > 240) throw Error('disposable localnet did not become ready'); await new Promise((r) => setTimeout(r, 500)); }
    }
    const chain = toHex(fromBase58(chainId).slice(0, 4));
    const owner = Ed25519Keypair.generate(), devices = Array.from({ length: DEVICES }, () => Ed25519Keypair.generate());
    for (let attempt = 0; ; attempt++) {
      try { for (const signer of [owner, ...devices]) await requestSuiFromFaucetV2({ host: process.env.PROTOCOL_TEST_FAUCET, recipient: signer.toSuiAddress() }); break; }
      catch (error) { if (attempt > 60) throw error; await new Promise((r) => setTimeout(r, 1000)); }
    }
    const wallet = path.join(tmp, 'wallet'); await fs.mkdir(wallet); await fs.writeFile(path.join(wallet, 'sui.keystore'), '[]');
    await fs.writeFile(path.join(wallet, 'client.yaml'), `keystore:\n  File: ${wallet}/sui.keystore\nenvs:\n- alias: localnet\n  rpc: ${rpc}\nactive_env: localnet\nactive_address: null\n`);
    const env = { ...process.env, SUI_CONFIG_DIR: wallet };

    // Head tree from the working copy; base sources from the fixed base commit.
    const tree = path.join(tmp, 'contracts'); await fs.cp(path.join(root, 'contracts'), tree, { recursive: true, filter: (p) => !['build', 'Published.toml'].includes(path.basename(p)) });
    const baseTree = path.join(tmp, 'base'); await fs.mkdir(baseTree);
    assert.match(BASE, /^[0-9a-f]{40}$/, 'SF129_BASE_REF must be a full commit hash');
    const archive = execFileSync('git', ['-C', root, 'archive', BASE, 'contracts/sources', 'contracts/fleet_data/sources'], { maxBuffer: 64 * 1024 * 1024 });
    execFileSync('tar', ['-x', '-C', baseTree], { input: archive });
    const headCore = path.join(tmp, 'head-core-sources'); await fs.cp(path.join(tree, 'sources'), headCore, { recursive: true });
    const headData = await fs.readFile(path.join(tree, 'fleet_data/sources/fleet_data.move'));
    const cache = '/home/dev/.move/git', pin = 'https___github_com_MystenLabs_walrus_git_c68b530e0b1e522b4a3896190d883e4312a1cf0e';
    assert((await fs.readdir(cache)).includes(pin), 'pinned Walrus source cache missing');
    const deps = path.join(tmp, 'deps'); await fs.mkdir(deps);
    for (const name of ['wal', 'walrus']) await fs.cp(path.join(cache, pin, 'testnet-contracts', name), path.join(deps, name), { recursive: true, filter: (p) => !['build', 'Published.toml'].includes(path.basename(p)) });
    for (const directory of [tree, path.join(deps, 'wal'), path.join(deps, 'walrus'), path.join(tree, 'fleet_storage'), path.join(tree, 'fleet_data')]) {
      const file = path.join(directory, 'Move.toml'); let text = (await fs.readFile(file, 'utf8')).replaceAll('rev = "testnet-v1.82.1"', 'rev = "987123842623d6cb0e772dc665789cfd69bdec9f"');
      text = text.includes('[environments]') ? text.replace('[environments]', `[environments]\nsf129 = "${chain}"`) : text + `\n[environments]\nsf129 = "${chain}"\n`;
      for (const name of ['wal', 'walrus']) text = text.replace(new RegExp(`^${name} = \\{ git = [^\\n]+$`, 'm'), `${name} = { local = "${deps}/${name}" }`);
      await fs.writeFile(file, text);
    }
    // Isolated one-member Walrus system (as SF-090/SF-092) plus an epoch
    // setter. All System/Storage/Blob mutation code remains upstream.
    await fs.appendFile(path.join(deps, 'walrus/sources/system/system_state_inner.move'), `
fun reclaim_committee(epoch: u32): BlsCommittee {
    let member = bls_aggregate::new_bls_committee_member(
        sui::bls12381::g1_to_uncompressed_g1(&sui::bls12381::g1_generator()),
        100, object::id_from_address(@0xabc));
    bls_aggregate::new_bls_committee(epoch, vector[member])
}
public(package) fun reclaim_fixture(ctx: &mut TxContext): SystemStateInnerV1 {
    let mut state = create_empty(104, ctx);
    state.committee = reclaim_committee(0);
    state.total_capacity_size = 1_000_000_000;
    state.storage_price_per_unit_size = 5;
    state.write_price_per_unit_size = 1;
    state
}
public(package) fun reclaim_fixture_epoch(self: &mut SystemStateInnerV1, epoch: u32) { self.committee = reclaim_committee(epoch); }
`);
    await fs.appendFile(path.join(deps, 'walrus/sources/system.move'), `
public fun reclaim_fixture(ctx: &mut TxContext) {
    let mut system = System { id: object::new(ctx), version: VERSION,
        package_id: object::id_from_address(@0xabc), new_package_id: option::none() };
    dynamic_field::add(&mut system.id, VERSION, system_state_inner::reclaim_fixture(ctx));
    transfer::share_object(system);
}
public fun reclaim_fixture_epoch(system: &mut System, epoch: u32) { system.inner_mut().reclaim_fixture_epoch(epoch) }
`);
    function compile(directory) {
      let text; try { text = execFileSync('sui', ['move', 'build', '--path', directory, '--build-env', 'sf129', '--no-tree-shaking', '--dump-bytecode-as-base64'], { env, encoding: 'utf8', maxBuffer: 30 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] }); }
      catch (error) { throw Error('isolated source compilation failed: ' + String(error.stderr).slice(-1500)); }
      return JSON.parse(text.slice(text.indexOf('{')));
    }
    const transactions = [];
    const gasOf = (r) => r.effects.gasUsed, net = (g) => BigInt(g.computationCost) + BigInt(g.storageCost) - BigInt(g.storageRebate);
    async function execute(label, tx, signer = owner) {
      const r = await client.signAndExecuteTransaction({ transaction: tx, signer, include: { effects: true, objectTypes: true, events: true } });
      assert(r.Transaction?.status.success, `${label}: ${JSON.stringify(r.FailedTransaction?.status)}`);
      await client.waitForTransaction({ digest: r.Transaction.digest });
      transactions.push({ label, digest: r.Transaction.digest, gas: gasOf(r.Transaction) });
      return r.Transaction;
    }
    /** Fully resolved inputs: rejection is a real failed effect, not a simulation. */
    async function executeFailing(label, build, signer, pattern) {
      const tx = new Transaction(); tx.setSender(signer.toSuiAddress());
      const coin = (await client.listCoins({ owner: signer.toSuiAddress() })).objects[0];
      tx.setGasPayment([{ objectId: coin.objectId, version: coin.version, digest: coin.digest }]);
      tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice); tx.setGasBudget(50_000_000);
      const shared = async (objectId, mutable = false) => { const o = (await client.getObject({ objectId })).object; return tx.sharedObjectRef({ objectId, initialSharedVersion: o.owner.Shared.initialSharedVersion, mutable }); };
      const owned = async (objectId) => { const o = (await client.getObject({ objectId })).object; return tx.objectRef({ objectId, version: o.version, digest: o.digest }); };
      await build(tx, shared, owned);
      const bytes = await tx.build(), signed = await signer.signTransaction(bytes);
      const result = await client.executeTransaction({ transaction: bytes, signatures: [signed.signature], include: { effects: true } });
      assert.equal(result.$kind, 'FailedTransaction', label); assert.match(JSON.stringify(result.FailedTransaction.status), pattern, label);
      await client.waitForTransaction({ digest: result.FailedTransaction.digest });
      return { label, digest: result.FailedTransaction.digest, status: result.FailedTransaction.status, gas: result.FailedTransaction.effects.gasUsed };
    }
    const created = (r, suffix) => Object.keys(r.objectTypes).filter((k) => r.objectTypes[k].endsWith(suffix));
    const one = (r, suffix) => { const all = created(r, suffix); assert.equal(all.length, 1, suffix); return all[0]; };
    const packageOf = (r) => r.effects.changedObjects.find((o) => o.outputState === 'PackageWrite').objectId;
    async function published(directory, pkg, original, version, upgradeCap) {
      await fs.writeFile(path.join(directory, 'Published.toml'), `[published.sf129]\nchain-id = "${chain}"\npublished-at = "${pkg}"\noriginal-id = "${original}"\nversion = ${version}\ntoolchain-version = "1.81.1"\nbuild-config = { flavor = "sui", edition = "2024" }\nupgrade-capability = "${upgradeCap}"\n`);
    }
    async function publish(name, directory) {
      const tx = new Transaction(), [cap] = tx.publish(compile(directory)); tx.transferObjects([cap], owner.toSuiAddress());
      const r = await execute('publish ' + name, tx), pkg = packageOf(r), upgradeCap = one(r, '::package::UpgradeCap');
      await published(directory, pkg, pkg, 1, upgradeCap); return { pkg, upgradeCap, r };
    }
    async function upgrade(name, directory, base) {
      await fs.rm(path.join(directory, 'Published.toml'));
      const built = compile(directory), tx = new Transaction();
      const [ticket] = tx.moveCall({ target: '0x2::package::authorize_upgrade', arguments: [tx.object(base.upgradeCap), tx.pure.u8(0), tx.pure.vector('u8', built.digest)] });
      const receipt = tx.upgrade({ modules: built.modules, dependencies: built.dependencies, package: base.pkg, ticket });
      tx.moveCall({ target: '0x2::package::commit_upgrade', arguments: [tx.object(base.upgradeCap), receipt] });
      const head = packageOf(await execute('upgrade ' + name, tx));
      await published(directory, head, base.pkg, 2, base.upgradeCap); return head;
    }
    async function origins(pkg) {
      const { object } = await client.getObject({ objectId: pkg, include: { objectBcs: true } });
      return Object.fromEntries(bcs.Object.parse(object.objectBcs).data.Package.typeOriginTable.map((o) => [`${o.moduleName}::${o.datatypeName}`, id(o.package)]));
    }

    // ── Base packages and existing objects ──────────────────────────────────
    await fs.rm(path.join(tree, 'sources'), { recursive: true }); await fs.cp(path.join(baseTree, 'contracts/sources'), path.join(tree, 'sources'), { recursive: true });
    const core = await publish('base core', tree), registry = one(core.r, '::device_registry::DeviceRegistry');
    const walPackage = await publish('WAL', path.join(deps, 'wal')), walrusPackage = await publish('Walrus', path.join(deps, 'walrus'));
    let tx = new Transaction(); tx.moveCall({ target: `${walrusPackage.pkg}::system::reclaim_fixture` });
    const system = one(await execute('initialize isolated Walrus system', tx), '::system::System');
    const storagePackage = await publish('storage', path.join(tree, 'fleet_storage'));
    const dataTree = path.join(tree, 'fleet_data');
    await fs.writeFile(path.join(dataTree, 'sources/fleet_data.move'), await fs.readFile(path.join(baseTree, 'contracts/fleet_data/sources/fleet_data.move')));
    const dataBase = await publish('base data', dataTree);

    tx = new Transaction(); const [deposit] = tx.splitCoins(tx.gas, [tx.pure.u64(2_000_000_000)]);
    const [limits] = tx.moveCall({ target: `${core.pkg}::fleet_budget::new_limits`, arguments: [tx.pure.u64(50_000_000), tx.pure.u64(86_400_000), tx.pure.u64(10_000_000), tx.pure.u64(4), tx.pure.u64(0)] });
    const [budgetCapResult] = tx.moveCall({ target: `${core.pkg}::fleet_budget::create`, arguments: [tx.object(registry), deposit, limits, tx.pure.u64(0)] }); tx.transferObjects([budgetCapResult], owner.toSuiAddress());
    let r = await execute('create budget', tx); const budget = one(r, '::fleet_budget::FleetBudget'), budgetCap = one(r, '::fleet_budget::BudgetOwnerCap');
    tx = new Transaction(); const [quota] = tx.moveCall({ target: `${storagePackage.pkg}::fleet_storage::new_quota`, arguments: [tx.pure.u64(86_400_000), tx.pure.u64(1_000_000_000), tx.pure.u64(4), tx.pure.u64(4_194_304), tx.pure.u64(1_000_000_000)] });
    tx.moveCall({ target: `${storagePackage.pkg}::fleet_storage::create`, arguments: [tx.object(budget), tx.object(budgetCap), quota] });
    const storage = one(await execute('create fleet storage', tx), '::fleet_storage::FleetStorage');
    tx = new Transaction(); const walCoin = one(walPackage.r, `::coin::Coin<${walPackage.pkg}::wal::WAL>`);
    const [wal] = tx.splitCoins(tx.object(walCoin), [tx.pure.u64(1_000_000_000)]);
    tx.moveCall({ target: `${storagePackage.pkg}::fleet_storage::deposit_wal`, arguments: [tx.object(storage), tx.object(budget), tx.object(budgetCap), wal] });
    const [nShards] = tx.moveCall({ target: `${walrusPackage.pkg}::system::n_shards`, arguments: [tx.object(system)] });
    const [blobBytes] = tx.moveCall({ target: `${walrusPackage.pkg}::encoding::encoded_blob_length`, arguments: [tx.pure.u64(100), tx.pure.u8(1), nShards] });
    // Two archives per device plus two unanchored uploads.
    for (let i = 0; i < 2 * DEVICES + 2; i++) tx.moveCall({ target: `${storagePackage.pkg}::fleet_storage::buy_storage`, arguments: [tx.object(storage), tx.object(budget), tx.object(budgetCap), tx.object(system), blobBytes, tx.pure.u32(STORAGE_EPOCHS)] });
    await execute('buy archive storage', tx);
    tx = new Transaction(); tx.moveCall({ target: `${dataBase.pkg}::fleet_data::create`, arguments: [tx.object(budget), tx.object(budgetCap), tx.object(storage), tx.pure.string(chain)] });
    const fleet = one(await execute('create fleet data', tx), '::fleet_data::FleetData');

    const fleetDevices = [];
    for (let i = 0; i < DEVICES; i++) {
      const key = devices[i];
      tx = new Transaction(); const [device] = tx.moveCall({ target: `${core.pkg}::device_registry::register_device`, arguments: [tx.object(registry), tx.pure.string(`reclaim-${i}`), tx.pure.address(key.toSuiAddress()), tx.pure.string('x86_64'), tx.object('0x6')] });
      tx.moveCall({ target: `${core.pkg}::device_cap::issue_cap`, arguments: [device] });
      const [oc] = tx.moveCall({ target: `${core.pkg}::device_registry::activate_device`, arguments: [tx.object(registry), device] });
      tx.moveCall({ target: `${core.pkg}::fleet_budget::enroll_device`, arguments: [tx.object(budget), tx.object(budgetCap), tx.object(registry), oc, tx.pure.u64(50_000_000), tx.pure.u64(0), tx.object('0x6')] });
      tx.transferObjects([oc], owner.toSuiAddress());
      r = await execute(`enroll device ${i}`, tx);
      const d = { key, deviceId: one(r, '::device_registry::Device'), cap: one(r, '::device_cap::DeviceCap'), account: one(r, '::fleet_budget::GasAccount') };
      tx = new Transaction(); tx.moveCall({ target: `${dataBase.pkg}::fleet_data::enroll`, arguments: [tx.object(fleet), tx.object(budget), tx.object(budgetCap), tx.object(d.account)] });
      d.writer = one(await execute(`enroll writer ${i}`, tx), '::fleet_data::DeviceData');
      fleetDevices.push(d);
    }

    const RecordBcs = bcs.struct('BlobRecord', { device_id: bcs.option(bcs.Address), blob_id: bcs.u256(), size: bcs.u64(), encoded_size: bcs.u64(), end_epoch: bcs.u32(), uploaded_at_ms: bcs.u64() });
    const secretKey = new Uint8Array(32); secretKey[31] = 1; // the fixture member's public key is the G1 generator
    const u32 = (n) => bcs.u32().serialize(n).toBytes(), u256 = (n) => bcs.u256().serialize(n).toBytes();
    async function upload(d, root, label) {
      let tx = new Transaction();
      const [blobId] = tx.moveCall({ target: `${walrusPackage.pkg}::blob::derive_blob_id`, arguments: [tx.pure.u256(root), tx.pure.u8(1), tx.pure.u64(100)] });
      tx.moveCall({ target: `${storagePackage.pkg}::fleet_storage::register_blob`, arguments: [tx.object(storage), tx.object(budget), tx.object(d.account), tx.object(registry), tx.object(d.cap), tx.object(system), blobId, tx.pure.u256(root), tx.pure.u64(100), tx.pure.u8(1), tx.object('0x6')] });
      const registered = await execute(`${label}: register`, tx, d.key), blob = one(registered, '::blob::Blob');
      const field = await client.getDynamicField({ parentId: storage, name: { type: `${storagePackage.pkg}::fleet_storage::RecordKey`, bcs: bcs.Address.serialize(blob).toBytes() } });
      const record = RecordBcs.parse(field.dynamicField.value.bcs);
      const message = new Uint8Array([1, 0, 3, ...u32(0), ...u256(record.blob_id), 0]);
      const signature = bls12_381.longSignatures.sign(bls12_381.longSignatures.hash(message), secretKey).toBytes();
      tx = new Transaction();
      tx.moveCall({ target: `${storagePackage.pkg}::fleet_storage::certify_blob`, arguments: [tx.object(storage), tx.object(system), tx.pure.address(blob), tx.pure.vector('u8', Array.from(signature)), tx.pure.vector('u8', [1]), tx.pure.vector('u8', Array.from(message))] });
      const certified = await execute(`${label}: certify`, tx, d.key);
      return { blob, record, registered, certified };
    }
    async function anchor(d, blob, seq, kind, label, pkg = dataBase.pkg) {
      const tx = new Transaction();
      tx.moveCall({ target: `${pkg}::fleet_data::anchor`, arguments: [tx.object(fleet), tx.object(d.writer), tx.object(budget), tx.object(d.account), tx.object(registry), tx.object(d.cap), tx.object(storage), tx.pure.address(blob), tx.pure.u64(seq), tx.pure.u8(kind), tx.pure.vector('u8', Array.from(createHash('sha256').update(blob).digest())), tx.object('0x6')] });
      return execute(`${label}: anchor`, tx, d.key);
    }
    // Every device: one periodic archive and one diagnostic archive, anchored
    // under the base package. Device 0 also keeps one unanchored upload.
    const archives = [];
    for (const [i, d] of fleetDevices.entries()) {
      for (const [kind, seq] of [[0, 1], [1, 2]]) {
        const u = await upload(d, 1000 + 10 * i + kind, `device ${i} archive ${seq}`);
        const anchored = await anchor(d, u.blob, seq, kind, `device ${i} archive ${seq}`);
        archives.push({ device: i, kind, ...u, anchored });
      }
    }
    // The unanchored blob is the second purchased slot of device 0's quota period.
    const loose = await upload(fleetDevices[0], 1999, 'device 0 unanchored upload').catch((error) => { throw Error('unanchored upload failed: ' + error.message); });

    // Deployment records under the base core package.
    tx = new Transaction(); tx.moveCall({ target: `${core.pkg}::device_group::create_group`, arguments: [tx.pure.string('reclaim'), tx.pure.string(''), tx.object('0x6')] });
    const group = one(await execute('create group', tx), '::device_group::DeviceGroup');
    const recipe = 'a'.repeat(64);
    async function deploy(version) {
      const tx = new Transaction();
      tx.moveCall({ target: `${core.pkg}::deployment_manager::create_deployment_verified`, arguments: [tx.pure.string('app'), tx.pure.string(version), tx.pure.string('recipe-blob'), tx.pure.string('b'.repeat(64)), tx.pure.string(recipe), tx.pure.vector('address', [group]), tx.pure.vector('address', []), tx.object('0x6')] });
      return one(await execute(`create deployment ${version}`, tx), '::deployment_manager::DeploymentRecord');
    }
    async function desire(record) {
      const tx = new Transaction(); const [plan] = tx.moveCall({ target: `${core.pkg}::desired_state::begin`, arguments: [tx.object(group)] });
      tx.moveCall({ target: `${core.pkg}::desired_state::set_component`, arguments: [plan, tx.object(record)] });
      tx.moveCall({ target: `${core.pkg}::desired_state::commit`, arguments: [plan, tx.object(group), tx.object('0x6')] });
      await execute('desire ' + record, tx);
    }
    const superseded = await deploy('1.0.0'); await desire(superseded);
    const current = await deploy('1.1.0'); await desire(current);
    const oneShot = await deploy('0.9.0');
    tx = new Transaction();
    for (const record of [superseded, current, oneShot]) tx.moveCall({ target: `${core.pkg}::deployment_manager::set_control`, arguments: [tx.object(record), tx.pure.u8(2), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0)] });
    await execute('cancel deployment records', tx);
    const before = await Promise.all([fleet, fleetDevices[0].writer, superseded].map(async (objectId) => (await client.getObject({ objectId, include: { content: true } })).object.content));

    // ── In-place upgrades of the existing packages ──────────────────────────
    await fs.rm(path.join(tree, 'sources'), { recursive: true }); await fs.cp(headCore, path.join(tree, 'sources'), { recursive: true });
    const coreHead = await upgrade('core', tree, core);
    await fs.writeFile(path.join(dataTree, 'sources/fleet_data.move'), headData);
    const dataHead = await upgrade('data', dataTree, dataBase);
    const dataOrigins = await origins(dataHead), coreOrigins = await origins(coreHead);
    assert.equal(dataOrigins['fleet_data::FleetData'], id(dataBase.pkg)); assert.equal(dataOrigins['fleet_data::AnchorKey'], id(dataBase.pkg));
    assert.equal(dataOrigins['fleet_data::AnchorsPruned'], id(dataHead)); assert.equal(dataOrigins['fleet_data::ExpiredBlobsBurned'], id(dataHead));
    assert.equal(coreOrigins['deployment_manager::DeploymentRecord'], id(core.pkg)); assert.equal(coreOrigins['desired_state::DesiredState'], id(core.pkg));
    assert.equal(coreOrigins['desired_state::Retirement'], id(coreHead));
    for (const [i, objectId] of [fleet, fleetDevices[0].writer, superseded].entries()) assert.deepEqual((await client.getObject({ objectId, include: { content: true } })).object.content, before[i], 'upgrade must not change existing objects');
    // Old package versions have no reclaim entry points.
    await assert.rejects(client.getMoveFunction({ packageId: dataBase.pkg, moduleName: 'fleet_data', name: 'prune_expired_anchors' }));
    // Devices keep registering through the existing storage after the upgrade;
    // this upload stays unanchored and is reclaimed as a loose expired blob.
    const late = await upload(fleetDevices[1], 2999, 'device 1 post-upgrade upload');

    // ── Reclaim through the dApp planner, with dry-run previews ─────────────
    const manifest = JSON.parse(await fs.readFile(path.join(root, 'manifests/fixture.localnet.json')));
    manifest.chain_identifier = chain; manifest.call_package_id = coreHead; manifest.shared_objects.registry.id = registry;
    for (const name of Object.keys(manifest.type_origins)) manifest.type_origins[name] = core.pkg;
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
    const load = loader(), reclaim = load('lib/storage-reclaim.ts'), data = load('lib/fleet-data.ts'), bulk = load('lib/fleet-bulk.ts');
    const config = data.parseDataConfig(JSON.stringify({ schema: 1, chainIdentifier: chain, registryId: registry, packageId: dataHead, typeOrigin: dataBase.pkg, fleetId: fleet, budgetId: budget, storageId: storage }));
    const deviceIds = fleetDevices.map((d) => d.deviceId);
    // Before expiry nothing is selected, and a forced prune fails on chain.
    let plan = await reclaim.planArchiveReclaim(client, config, storagePackage.pkg, deviceIds, 0);
    assert.deepEqual([plan.anchors.length, plan.loose.length, plan.scanned], [0, 0, 2 * DEVICES]);
    const pruneArgs = async (tx, shared, owned) => [await shared(fleet), await shared(fleetDevices[0].writer, true), await shared(budget), await owned(budgetCap), await shared(storage, true), await shared(system), tx.pure.vector('u64', [0])];
    const unexpired = await executeFailing('prune before expiry', async (tx, shared, owned) => tx.moveCall({ target: `${dataHead}::fleet_data::prune_expired_anchors`, arguments: await pruneArgs(tx, shared, owned) }), owner, /burn_expired.*8|MoveAbort.*8/);
    const unexpiredBurn = await executeFailing('burn before expiry', async (tx, shared, owned) => tx.moveCall({ target: `${dataHead}::fleet_data::burn_expired_blobs`, arguments: [await shared(fleet), await shared(budget), await owned(budgetCap), await shared(storage, true), await shared(system), tx.pure.vector('address', [loose.blob])] }), owner, /burn_expired.*8|MoveAbort.*8/);
    // A device cannot present the owner's cap: the transaction is rejected before execution.
    const intruder = await executeFailing('device cannot prune', async (tx, shared, owned) => tx.moveCall({ target: `${dataHead}::fleet_data::prune_expired_anchors`, arguments: await pruneArgs(tx, shared, owned) }), fleetDevices[0].key, /.+/)
      .then(() => { throw Error('device used the owner cap'); }, (error) => ({ label: 'device cannot use the owner cap', refused: String(error.message).slice(0, 200) }));

    const endEpoch = archives[0].record.end_epoch;
    tx = new Transaction(); tx.moveCall({ target: `${walrusPackage.pkg}::system::reclaim_fixture_epoch`, arguments: [tx.object(system), tx.pure.u32(endEpoch)] });
    await execute(`advance Walrus fixture to epoch ${endEpoch}`, tx);
    plan = await reclaim.planArchiveReclaim(client, config, storagePackage.pkg, deviceIds, endEpoch);
    assert.equal(plan.anchors.length, 2 * DEVICES); assert(plan.anchors.every((c) => c.held));
    assert.deepEqual(plan.loose.sort(), [loose.blob, late.blob].map((blob) => id(blob)).sort());

    // Single archive: device 0's periodic log.
    const single = { ...plan, anchors: plan.anchors.filter((c) => c.deviceId === id(fleetDevices[0].deviceId) && c.index === 0), loose: [] };
    const singlePreview = await reclaim.previewReclaim(client, reclaim.buildArchiveReclaimTx(config, budgetCap, system, single).tx, owner.toSuiAddress());
    assert(singlePreview.ok, singlePreview.error);
    const singleTx = await execute('reclaim one expired archive', reclaim.buildArchiveReclaimTx(config, budgetCap, system, single).tx);
    const singleNet = -net(gasOf(singleTx));
    assert.equal(singlePreview.netMist, singleNet, 'dry run must equal executed net return');
    assert.equal(await bulk.readAnchorAt(client, config, fleetDevices[0].deviceId, 0), undefined);
    assert.equal((await bulk.readAnchorEntries(client, config, fleetDevices[0].deviceId, 0, 8)).entries.map((e) => e.index).join(), '1');
    // The remaining archives in one batched transaction, then the unanchored blobs.
    plan = await reclaim.planArchiveReclaim(client, config, storagePackage.pkg, deviceIds, endEpoch);
    assert.equal(plan.anchors.length, 2 * DEVICES - 1);
    async function reclaimed(label, part) {
      const preview = await reclaim.previewReclaim(client, reclaim.buildArchiveReclaimTx(config, budgetCap, system, part).tx, owner.toSuiAddress());
      assert(preview.ok, preview.error);
      const built = reclaim.buildArchiveReclaimTx(config, budgetCap, system, part), executed = await execute(label, built.tx);
      assert.equal(preview.netMist, -net(gasOf(executed)), 'dry run must equal executed net return');
      return { preview, built, executed, removals: BigInt(built.anchors + built.loose) };
    }
    const batch = await reclaimed('reclaim remaining expired archives', { ...plan, loose: [] });
    const looseBatch = await reclaimed('burn unanchored expired blobs', { ...plan, anchors: [] });
    const batchPreview = batch.preview, batchTx = batch.executed;
    plan = await reclaim.planArchiveReclaim(client, config, storagePackage.pkg, deviceIds, endEpoch);
    assert.deepEqual([plan.anchors.length, plan.loose.length, plan.scanned], [0, 0, 0]);
    const storageFields = (await client.listDynamicFields({ parentId: storage })).dynamicFields.filter((f) => /RecordKey|BlobKey/.test(f.name.type));
    assert.equal(storageFields.length, 0, 'every expired blob and record was removed');
    for (const d of fleetDevices) assert.equal((await client.listDynamicFields({ parentId: d.writer })).dynamicFields.length, 0, 'anchors and reuse markers removed');
    const reanchor = await executeFailing('burned blob cannot be re-anchored', async (tx, shared, owned) => tx.moveCall({ target: `${dataHead}::fleet_data::anchor`, arguments: [await shared(fleet), await shared(fleetDevices[2].writer, true), await shared(budget), await shared(fleetDevices[2].account), await shared(registry), await owned(fleetDevices[2].cap), await shared(storage), tx.pure.address(archives[4].blob), tx.pure.u64(9), tx.pure.u8(0), tx.pure.vector('u8', Array(32).fill(9)), await shared('0x6')] }), fleetDevices[2].key, /MoveAbort/);

    // Deployment records: superseded and one-shot are retired, the desired one kept.
    const events = [superseded, current, oneShot].map((deploymentId, i) => ({ deploymentId, schemaVersion: '1', componentName: 'app', version: ['1.0.0', '1.1.0', '0.9.0'][i], recipeBlobId: 'recipe-blob', artifactSha256: 'b'.repeat(64),
      targetGroups: [group], targetDevices: [], creator: owner.toSuiAddress(), timestampMs: 0, statuses: {}, receiptsVerified: true, resolvedDeviceIds: [],
      control: { revision: '1', state: 2, not_before_ms: '0', expires_ms: '0', window_start_ms: '0', window_end_ms: '0', notice_ms: '0' } }));
    const deploymentPlan = await reclaim.planDeploymentReclaim(client, events, owner.toSuiAddress());
    assert.deepEqual(deploymentPlan.map((p) => [p.deployment.version, p.eligible, p.groups.length]), [['1.0.0', true, 1], ['1.1.0', false, 1], ['0.9.0', true, 0]]);
    const desiredRefused = await executeFailing('still-desired record refused', async (tx, shared) => {
      const record = await shared(current, true), groupRef = await shared(group);
      const [retirement] = tx.moveCall({ target: `${coreHead}::desired_state::begin_retirement`, arguments: [record] });
      tx.moveCall({ target: `${coreHead}::desired_state::confirm_not_desired`, arguments: [retirement, groupRef] });
      tx.moveCall({ target: `${coreHead}::desired_state::retire`, arguments: [retirement, record] });
    }, owner, /confirm_not_desired.*7|MoveAbort.*7/);
    const retirePreview = await reclaim.previewReclaim(client, reclaim.buildRetireDeploymentsTx(coreHead, deploymentPlan), owner.toSuiAddress());
    assert(retirePreview.ok, retirePreview.error);
    const retireTx = await execute('retire finished deployment records', reclaim.buildRetireDeploymentsTx(coreHead, deploymentPlan));
    assert.equal(retirePreview.netMist, -net(gasOf(retireTx)));
    for (const [objectId, kept] of [[superseded, false], [current, true], [oneShot, false]]) {
      const exists = await client.getObject({ objectId }).then(() => true, () => false);
      assert.equal(exists, kept, objectId);
    }

    // ── Measurements ────────────────────────────────────────────────────────
    const sum = (items, f) => items.reduce((total, item) => total + f(item), BigInt(0));
    const periodic = archives.filter((a) => a.kind === 0);
    const avg = (items, f) => sum(items, f) / BigInt(items.length);
    const register = avg(periodic, (a) => net(gasOf(a.registered))), certify = avg(periodic, (a) => net(gasOf(a.certified))), anchored = avg(periodic, (a) => net(gasOf(a.anchored)));
    // One purchase transaction bought every slot. Per-transaction nets include
    // rewrites of shared objects (System, FleetStorage, writers), so the robust
    // figures are the whole-fixture lifecycle and the deposit actually returned.
    const purchase = transactions.find((t) => t.label === 'buy archive storage').gas, blobs = BigInt(2 * DEVICES + 2);
    const purchasePerArchive = net(purchase) / blobs;
    const writePerArchive = purchasePerArchive + register + certify + anchored;
    const batchPerArchive = -net(gasOf(batchTx)) / batch.removals;
    const loosePerBlob = -net(gasOf(looseBatch.executed)) / looseBatch.removals;
    const lifecycle = transactions.filter((t) => /buy archive storage|: register|: certify|: anchor|reclaim one|reclaim remaining|burn unanchored/.test(t.label));
    const lifecycleNet = sum(lifecycle, (t) => net(t.gas)), lifecycleComputation = sum(lifecycle, (t) => BigInt(t.gas.computationCost));
    // Each live archive holds at least the deposit its reclaim returns.
    const lockedPerArchive = batchPerArchive;
    const weekly = { mainnet: { epochDays: 14 }, testnet: { epochDays: 1 } };
    for (const [network, w] of Object.entries(weekly)) {
      const retentionDays = STORAGE_EPOCHS * w.epochDays;
      w.retentionDays = retentionDays; w.liveArchivesPerDevice = Math.ceil(retentionDays / 7);
      w.lockedDepositPerDeviceMist = (lockedPerArchive * BigInt(w.liveArchivesPerDevice)).toString();
      w.lockedDepositAfterOneYearWithoutReclaimMist = (lockedPerArchive * BigInt(52)).toString();
      void network;
    }
    const result = {
      schema: 1, mode: 'isolated-network-none-localnet', result: 'PASS', revision: execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(), base: BASE, chain,
      referenceGasPrice: String((await client.getReferenceGasPrice()).referenceGasPrice),
      packages: { coreBase: core.pkg, coreHead, dataBase: dataBase.pkg, dataHead, storage: storagePackage.pkg, walrus: walrusPackage.pkg },
      typeOrigins: { 'fleet_data::FleetData': dataOrigins['fleet_data::FleetData'], 'fleet_data::AnchorsPruned': dataOrigins['fleet_data::AnchorsPruned'],
        'fleet_data::ExpiredBlobsBurned': dataOrigins['fleet_data::ExpiredBlobsBurned'], 'desired_state::Retirement': coreOrigins['desired_state::Retirement'],
        'deployment_manager::DeploymentRecord': coreOrigins['deployment_manager::DeploymentRecord'] },
      measuredMist: {
        perWeeklyArchive: { storagePurchaseShare: purchasePerArchive.toString(), register: register.toString(), certify: certify.toString(), anchor: anchored.toString(), writeTotal: writePerArchive.toString(),
          lockedStorageDeposit: lockedPerArchive.toString(), reclaimReturnedSingle: singleNet.toString(),
          lifecycleNetPerBlob: (lifecycleNet / blobs).toString(), lifecycleComputationPerBlob: (lifecycleComputation / blobs).toString(), lifecycleBlobs: blobs.toString(), reclaimReturnedPerArchiveBatched: batchPerArchive.toString(), batchedArchives: batch.removals.toString(), reclaimReturnedPerUnanchoredBlob: loosePerBlob.toString(),
          netAfterReclaimBatched: (writePerArchive - batchPerArchive).toString(), netAfterReclaimSingle: (writePerArchive - singleNet).toString() },
        defaultRetention: { storageEpochs: STORAGE_EPOCHS, ...weekly },
        deploymentRecords: { retiredRecords: 2, returnedNet: (-net(gasOf(retireTx))).toString() },
      },
      previews: { single: singlePreview.netMist.toString(), batch: batchPreview.netMist.toString(), loose: looseBatch.preview.netMist.toString(), retire: retirePreview.netMist.toString(), matchedExecutedEffects: true },
      refusals: [unexpired, unexpiredBurn, intruder, reanchor, desiredRefused],
      transactions,
      limits: ['Isolated chain: storage and computation follow the pinned 1.82.1 protocol; it is not a public-network price.',
        'Walrus System is a one-member fixture whose epoch is set directly; certification uses a real BLS certificate for that committee.',
        'WAL storage/write fees are consumed by Walrus and are not refunded; only Sui object deposits are reclaimed.',
        'Mainnet live-archive counts assume 14-day Walrus epochs and weekly uploads at three purchased epochs.',
        'Per-transaction nets include rewrites of shared objects; reclaim returns exceed the write transactions\' own net deposits. Use the lifecycle and returned-deposit figures.'],
    };
    const replacer = (_, v) => typeof v === 'bigint' ? v.toString() : v;
    // Only an explicitly chosen receipt path is written; no shared temporary file.
    if (process.env.SF129_RECEIPT) await fs.writeFile(process.env.SF129_RECEIPT, JSON.stringify(result, replacer, 2) + '\n', { mode: 0o644 });
    console.log(JSON.stringify({ result: 'PASS', measuredMist: result.measuredMist }, replacer));
  } finally { await fs.rm(tmp, { recursive: true, force: true }); }
})().catch((e) => { console.error(e.stack); process.exitCode = 1; });
