// Existing Rollout + old-package calls across an actual compatible upgrade.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os');
const { execFileSync } = require('node:child_process');
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc'), { Transaction } = appRequire('@mysten/sui/transactions');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519'), { requestSuiFromFaucetV2 } = appRequire('@mysten/sui/faucet');
const { bcs } = appRequire('@mysten/sui/bcs'), { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
(async () => {
  assert.equal(process.env.PROTOCOL_TEST_DISPOSABLE, '1');
  assert.deepEqual((await fs.readdir('/sys/class/net')).sort(), ['lo']);
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'sf164-upgrade-'));
  try {
    const base = '0ca16547'; // Evaluated main before audit storage, independent of branch/head movement.
    const oldTree = path.join(tmp, 'base'); await fs.mkdir(oldTree);
    execFileSync('tar', ['-x', '-C', oldTree], { input: execFileSync('git', ['archive', base, 'contracts'], { maxBuffer: 25e6 }) });
    const wallet = path.join(tmp, 'wallet'); await fs.mkdir(wallet);
    await fs.writeFile(path.join(wallet, 'sui.keystore'), '[]');
    await fs.writeFile(path.join(wallet, 'client.yaml'), `keystore:\n  File: ${wallet}/sui.keystore\nenvs:\n- alias: testnet\n  rpc: http://127.0.0.1:9000\nactive_env: testnet\nactive_address: null\n`);
    const compile = directory => {
      const out = execFileSync('sui', ['move', 'build', '--path', directory, '--build-env', 'testnet', '--no-tree-shaking', '--dump-bytecode-as-base64'], { env: { ...process.env, SUI_CONFIG_DIR: wallet }, encoding: 'utf8', maxBuffer: 25e6, stdio: ['ignore', 'pipe', 'pipe'] });
      return JSON.parse(out.slice(out.indexOf('{')));
    };
    const client = new SuiGrpcClient({ network: 'localnet', baseUrl: 'http://127.0.0.1:9000' }), owner = Ed25519Keypair.generate();
    for (let n = 0; n < 5; n++) await requestSuiFromFaucetV2({ host: 'http://127.0.0.1:9123', recipient: owner.toSuiAddress() });
    const digests = [];
    const execute = async (tx) => {
      tx.setGasBudget(900000000);
      const r = await client.signAndExecuteTransaction({ transaction: tx, signer: owner, include: { effects: true, objectTypes: true } });
      assert.ok(r.Transaction?.status.success, JSON.stringify(r.FailedTransaction?.status));
      await client.waitForTransaction({ digest: r.Transaction.digest }); digests.push(r.Transaction.digest); return r.Transaction;
    };
    const created = (r, suffix) => { const found = Object.keys(r.objectTypes).find(k => r.objectTypes[k].endsWith(suffix)); assert.ok(found, suffix); return found; };
    let tx = new Transaction(); const [capResult] = tx.publish(compile(path.join(oldTree, 'contracts'))); tx.transferObjects([capResult], owner.toSuiAddress());
    const first = await execute(tx), old = first.effects.changedObjects.find(o => o.outputState === 'PackageWrite').objectId;
    const cap = created(first, '::package::UpgradeCap');
    const make = pkg => { const t = new Transaction();
      const [policy] = t.moveCall({ target: `${pkg}::deployment_manager::rollout_policy`, arguments: [1,1,1,0,1,10000,1000].map(n => t.pure.u64(n)) });
      const [record] = t.moveCall({ target: `${pkg}::deployment_manager::new_rollout_verified`, arguments: [t.pure.string('audit'), t.pure.string('1'), t.pure.string('unused'), t.pure.string('a'.repeat(64)), t.pure.string('b'.repeat(64)), t.pure.vector('id', []), t.pure.vector('id', [id('0x1')]), t.pure.vector('u64', [0]), policy, t.object('0x6')] });
      t.moveCall({ target: `${pkg}::deployment_manager::share_deployment`, arguments: [record] }); return t; };
    const existing = created(await execute(make(old)), '::deployment_manager::DeploymentRecord');
    // Compile head with the old publication metadata for a genuine lineage upgrade.
    const headTree = path.join(tmp, 'head'); await fs.cp('contracts', headTree, { recursive: true });
    // A package's self address is resolved by Transaction.upgrade from the modules.
    await fs.rm(path.join(headTree, 'Published.toml'), { force: true });
    const built = compile(headTree);
    tx = new Transaction();
    const [ticket] = tx.moveCall({ target: '0x2::package::authorize_upgrade', arguments: [tx.object(cap), tx.pure.u8(0), tx.pure.vector('u8', built.digest)] });
    const receipt = tx.upgrade({ modules: built.modules, dependencies: built.dependencies, package: old, ticket });
    tx.moveCall({ target: '0x2::package::commit_upgrade', arguments: [tx.object(cap), receipt] });
    const head = (await execute(tx)).effects.changedObjects.find(o => o.outputState === 'PackageWrite').objectId;
    const manifest = JSON.parse(await fs.readFile('manifests/fixture.localnet.json', 'utf8'));
    for (const name of Object.keys(manifest.type_origins)) manifest.type_origins[name] = old;
    for (const name of ['DecisionKey', 'DecisionRecord', 'DecisionCleanupKey']) manifest.type_origins[`deployment_manager::${name}`] = head;
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
    const load = loader(), progress = load('lib/rollout-progress.ts');
    const control = async (pkg, record) => { const t = new Transaction(); t.moveCall({ target: `${pkg}::deployment_manager::control_rollout`, arguments: [t.object(record), t.pure.u8(1), t.pure.vector('id', []), t.pure.string('review hold'), t.object('0x6')] }); await execute(t); };
    await control(head, existing);
    const noEvents = { listDynamicFields: client.listDynamicFields.bind(client), async listEvents() { throw Error('requested data below earliest available'); } };
    let result = await progress.fetchRolloutDecisions(noEvents, existing, 2n);
    assert.deepEqual(result.missing, ['1']); assert.deepEqual(result.decisions.map(d => d.revision), [2n]);
    await control(old, existing); // Old packages remain callable and leave a precise new gap.
    await control(head, existing);
    result = await progress.fetchRolloutDecisions(noEvents, existing, 4n);
    assert.deepEqual(result.missing, ['1', '3']); assert.deepEqual(result.decisions.map(d => d.revision), [4n, 2n]);
    const fresh = created(await execute(make(head)), '::deployment_manager::DeploymentRecord');
    result = await progress.fetchRolloutDecisions(noEvents, fresh, 1n);
    assert.equal(result.complete, true);
    tx = new Transaction(); for (const record of [existing, fresh]) tx.moveCall({ target: `${head}::deployment_manager::delete_rollout`, arguments: [tx.object(record)] }); await execute(tx);
    console.log(JSON.stringify({ check: 'SF164 compatible existing-rollout upgrade, old-package gaps, stored reads with events pruned, cleanup', base, old, head, digests }));
  } finally { await fs.rm(tmp, { recursive: true, force: true }); }
})().catch(e => { console.error(e.message); process.exitCode = 1; });
