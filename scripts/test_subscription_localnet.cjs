// SF-181: real current-package -> subscription upgrade, existing Customer and
// rejection through old entry points. Disposable network-none only; no wallets.
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { appRequire } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { requestSuiFromFaucetV2 } = appRequire('@mysten/sui/faucet');
(async () => {
  assert.deepEqual((await fs.readdir('/sys/class/net')).sort(), ['lo']);
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'sf181-upgrade-'));
  try {
    const base = path.join(temporary, 'base');
    await fs.mkdir(base);
    execFileSync('tar', ['-x', '-C', base], { input: execFileSync('git', ['archive', '5331a8cb', 'contracts'], { maxBuffer: 25e6 }) });
    const env = { ...process.env, SUI_CONFIG_DIR: path.join(temporary, 'config') };
    await fs.mkdir(env.SUI_CONFIG_DIR);
    const keystore = path.join(env.SUI_CONFIG_DIR, 'empty.keystore');
    await fs.writeFile(keystore, '[]');
    await fs.writeFile(path.join(env.SUI_CONFIG_DIR, 'client.yaml'), `keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: http://127.0.0.1:9000\nactive_env: testnet\nactive_address: null\n`);
    function compile(directory) {
      const output = execFileSync('sui', ['move', 'build', '--path', directory, '--build-env', 'testnet', '--no-tree-shaking', '--dump-bytecode-as-base64'], { env, encoding: 'utf8', maxBuffer: 25e6, stdio: ['ignore', 'pipe', 'pipe'] });
      return JSON.parse(output.slice(output.indexOf('{')));
    }
    const client = new SuiGrpcClient({ network: 'localnet', baseUrl: 'http://127.0.0.1:9000' });
    const owner = Ed25519Keypair.generate();
    const payee = Ed25519Keypair.generate().toSuiAddress();
    for (let index = 0; index < 40; index++) {
      for (let retry = 0; ; retry++) {
        try { await requestSuiFromFaucetV2({ host: 'http://127.0.0.1:9123', recipient: owner.toSuiAddress() }); break; }
        catch (error) { if (retry === 9) throw Error(`disposable faucet: ${error.message}`); await new Promise(resolve => setTimeout(resolve, 500)); }
      }
    }
    const evidence = { base: '5331a8cb', contracts_tree: execFileSync('git', ['rev-parse', 'HEAD:contracts'], { encoding: 'utf8' }).trim(), transactions: [], limits: ['Disposable localnet SUI; no real USDC, public network, manual wallet, provider or physical acceptance.'] };
    async function execute(label, tx, failure) {
      tx.setGasBudget(label.includes('publish') || label.includes('upgrade') ? 2_000_000_000 : 100_000_000);
      if (failure) {
        const gas = (await client.listCoins({ owner: owner.toSuiAddress() })).objects[0];
        tx.setSender(owner.toSuiAddress());
        tx.setGasPayment([{ objectId: gas.objectId, version: gas.version, digest: gas.digest }]);
        tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice);
        const abi = JSON.parse(await fs.readFile('interfaces/fleet-v1.json', 'utf8')).modules;
        const data = tx.getData(), mutable = new Set();
        for (const { MoveCall: call } of data.commands) call.arguments.forEach((arg, index) => {
          if (arg.$kind === 'Input' && abi[call.module].functions[call.function].parameters[index].startsWith('&mut ')) mutable.add(arg.Input);
        });
        for (let index = 0; index < data.inputs.length; index++) {
          const id = data.inputs[index].UnresolvedObject?.objectId;
          if (!id) continue;
          const { object } = await client.getObject({ objectId: id });
          data.inputs[index] = object.owner.$kind === 'Shared'
            ? { Object: { SharedObject: { objectId: id, initialSharedVersion: object.owner.Shared.initialSharedVersion, mutable: mutable.has(index) } } }
            : { Object: { ImmOrOwnedObject: { objectId: id, version: object.version, digest: object.digest } } };
        }
        const bytes = await Transaction.from(JSON.stringify(data)).build();
        const signed = await owner.signTransaction(bytes);
        const result = await client.executeTransaction({ transaction: bytes, signatures: [signed.signature], include: { effects: true } });
        assert.ok(result.FailedTransaction, `${label} unexpectedly succeeded`);
        const output = result.FailedTransaction;
        await client.waitForTransaction({ digest: output.digest });
        assert.match(output.status.error?.message ?? JSON.stringify(output.status), failure, label);
        evidence.transactions.push({ label, digest: output.digest, status: output.status, gas: output.effects.gasUsed });
        return output;
      }
      const result = await client.signAndExecuteTransaction({ transaction: tx, signer: owner, include: { effects: true, events: true, objectTypes: true } }).catch(error => { throw Error(`${label}: ${error.message}`); });
      const output = result.Transaction ?? result.FailedTransaction;
      await client.waitForTransaction({ digest: output.digest });
      evidence.transactions.push({ label, digest: output.digest, status: output.status, gas: output.effects.gasUsed });
      if (failure) {
        assert.equal(result.$kind, 'FailedTransaction', label);
        assert.match(output.status.error?.message ?? JSON.stringify(output.status), failure, label);
      } else assert.equal(result.$kind, 'Transaction', `${label}: ${JSON.stringify(output.status)}`);
      return output;
    }
    function created(result, suffix) {
      const found = result.effects.changedObjects.filter(object => object.idOperation === 'Created' && result.objectTypes[object.objectId]?.split('<')[0].endsWith(suffix));
      assert.equal(found.length, 1, suffix);
      return found[0].objectId;
    }
    let tx = new Transaction();
    const [cap] = tx.publish(compile(path.join(base, 'contracts')));
    tx.transferObjects([cap], owner.toSuiAddress());
    const published = await execute('publish current base', tx);
    const old = published.effects.changedObjects.find(object => object.outputState === 'PackageWrite').objectId;
    const registry = created(published, '::device_registry::DeviceRegistry');
    const upgradeCap = created(published, '::package::UpgradeCap');
    tx = new Transaction();
    tx.moveCall({ target: `${old}::customer::create`, arguments: [tx.object(registry), tx.pure.string('Existing subscriber')] });
    const customer = created(await execute('create Customer before upgrade', tx), '::customer::Customer');
    const head = compile('contracts');
    tx = new Transaction();
    const [ticket] = tx.moveCall({ target: '0x2::package::authorize_upgrade', arguments: [tx.object(upgradeCap), tx.pure.u8(0), tx.pure.vector('u8', head.digest)] });
    const receipt = tx.upgrade({ modules: head.modules, dependencies: head.dependencies, package: old, ticket });
    tx.moveCall({ target: '0x2::package::commit_upgrade', arguments: [tx.object(upgradeCap), receipt] });
    const upgraded = await execute('compatible additive upgrade', tx);
    const current = upgraded.effects.changedObjects.find(object => object.outputState === 'PackageWrite').objectId;
    const coinType = '0x2::sui::SUI';
    function call(tx, name, args) { return tx.moveCall({ target: `${current}::subscription::${name}`, typeArguments: [coinType], arguments: args }); }
    tx = new Transaction();
    call(tx, 'offer', [tx.object(registry), tx.pure.address(payee), ...[1, 100, 1000, 100].map(n => tx.pure.u64(n)), tx.pure.bool(true), tx.object('0x6')]);
    const offer = created(await execute('immutable annual offer', tx), '::subscription::Offer');
    tx = new Transaction();
    const [deposit] = tx.splitCoins(tx.gas, [tx.pure.u64(1080)]);
    call(tx, 'open', [tx.object(customer), tx.object(registry), tx.object(offer), deposit, tx.object('0x6')]);
    const account = created(await execute('prepay existing Customer', tx), '::subscription::Account');
    tx = new Transaction();
    const [wrongCoin] = tx.moveCall({ target: '0x2::coin::zero', typeArguments: ['0x2::coin::Coin<0x2::sui::SUI>'] });
    call(tx, 'deposit', [tx.object(account), wrongCoin]);
    await assert.rejects(() => execute('wrong coin deposit', tx), /type|Type|mismatch/);
    evidence.wrong_coin_rejected = true;
    tx = new Transaction(); call(tx, 'collect', [tx.object(account), tx.object(customer), tx.object('0x6')]);
    await execute('collect first period', tx);
    tx = new Transaction(); call(tx, 'collect', [tx.object(account), tx.object(customer), tx.object('0x6')]);
    await execute('double collection rejected', tx, /abort code: 2, in '[^']*::subscription/);
    for (const [name, args, code] of [
      ['set_entitlement', t => [t.object(customer), t.object(registry), ...[999, 999, 999].map(n => t.pure.u64(n))], 1],
      ['set_credential', t => [t.object(customer), t.pure.address(payee), t.pure.bool(true), t.pure.bool(true)], 0],
    ]) {
      tx = new Transaction(); tx.moveCall({ target: `${old}::customer::${name}`, arguments: args(tx) });
      await execute(`old package ${name} rejected after opt-in`, tx, new RegExp(`abort code: ${code}, in '[^']*::customer`));
    }
    tx = new Transaction();
    const [refund] = call(tx, 'cancel', [tx.object(account), tx.object(customer), tx.object('0x6')]);
    tx.transferObjects([refund], owner.toSuiAddress());
    const cancelled = await execute('cancel and return unearned prepay', tx);
    const event = cancelled.events.find(event => event.eventType.includes('::subscription::Refunded'));
    assert.ok(event, 'refund event');
    // Read the account independently, not just a returned transaction digest.
    const accountRead = await client.core.getObjects({ objectIds: [account], include: { content: true } });
    assert.equal(accountRead.objects.length, 1);
    const { loader } = require('./frontend_test_utils.cjs');
    const load = loader();
    const protocol = load('lib/fleet-protocol.ts');
    const refundEvent = protocol.subscription_RefundedBcs.parse(event.bcs);
    assert.equal(BigInt(refundEvent.amount), 990n); assert.equal(refundEvent.recipient, owner.toSuiAddress());
    const wire = protocol.subscription_AccountBcs.parse(accountRead.objects[0].content);
    assert.equal(wire.treasury, payee); assert.equal(BigInt(wire.fee), 90n);
    assert.equal(wire.cancelled, true); assert.equal(BigInt(wire.balance.value), 0n); assert.equal(BigInt(wire.remaining), 0n);
    evidence.objects = { old, current, registry, customer, upgradeCap, offer, account, treasury: payee };
    if (process.env.SF181_EVIDENCE) await fs.writeFile(process.env.SF181_EVIDENCE, JSON.stringify(evidence, (_, value) => typeof value === 'bigint' ? value.toString() : value, 2) + '\n');
    console.log('PASS: current-base upgrade, existing Customer, fixed annual prepay, bounded collection, old-package entitlement/credential rejection and refunded zero balance');
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
})().then(() => process.exit(0), error => { console.error(error.message); process.exit(1); });
