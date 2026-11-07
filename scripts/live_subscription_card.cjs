// Keyless bridge: exercise actual card observation + SF-182 builders on Testnet.
const fs = require('node:fs'), readline = require('node:readline');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const [manifest, recipient, offerId] = process.argv.slice(2);
const m = JSON.parse(fs.readFileSync(manifest));
if (m.network !== 'testnet' || m.chain_identifier !== '4c78adac') throw Error('Testnet only');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const table = JSON.parse(fs.readFileSync('dapp/src/lib/fleet-price-table.json'));
table.source = 'SF-183 disposable test-only tariff (not a customer price)';
table.base.monthlyMinimumUsd = 0.01;
table.base.tiers = table.base.tiers.map(t => ({ ...t, usdPerDeviceMonth: 0.00001 }));
process.env.NEXT_PUBLIC_FLEET_PRICE_TABLE = JSON.stringify(table);
const load = loader(), card = load('lib/subscription-card.ts'), sub = load('lib/subscription.ts');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc'), { Transaction, coinWithBalance } = appRequire('@mysten/sui/transactions'), { toBase64 } = appRequire('@mysten/sui/utils');
const client = new SuiGrpcClient({ network: 'testnet', baseUrl: m.rpc_url });
const input = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();
let purchase, binding, quote;
(async () => {
  if (recipient === '--verify-evidence') {
    const e = JSON.parse(fs.readFileSync(offerId)), checks = [], transfers = [];
    const wallet = e.simulated_purchase.binding.wallet;
    const { normalizeStructTag } = appRequire('@mysten/sui/utils');
    await load('lib/deployment-manifest.ts').verifyDeployment(client);
    for (const t of [...e.transactions, ...e.child_transactions]) {
      if (t.status === 'not_submitted') continue;
      const r = await client.getTransaction({ digest: t.digest, include: { effects: true, balanceChanges: true } });
      const tx = r.Transaction ?? r.FailedTransaction;
      if (!tx || tx.status.success !== (t.status === 'success')) throw Error('Effects status mismatch');
      const g = tx.effects.gasUsed, net = BigInt(g.computationCost)+BigInt(g.storageCost)-BigInt(g.storageRebate);
      if ((net > 0n ? net : 0n).toString() !== String(t.gas_mist)) throw Error('Gas accounting mismatch');
      checks.push({ digest: t.digest, success: tx.status.success, signed_net_gas_mist: net.toString() });
      if (t.label.startsWith('SF-183 simulated purchase transfer')) {
        const amount = t.label.split(' ').at(-1);
        const change = tx.balanceChanges.find(c => c.address === wallet && normalizeStructTag(c.coinType) === normalizeStructTag(m.subscription_coins.USDC.coin_type));
        if (!change || change.amount !== amount) throw Error('Simulated purchase recipient/USDC amount mismatch');
        transfers.push({ digest: t.digest, recipient: wallet, amount: change.amount });
      }
    }
    if (transfers.map(t => t.amount).join(',') !== '1000,9000') throw Error('Missing partial/complete purchase transfers');
    await sub.verifyPlan(client, e.prepay.digest, e.simulated_purchase.binding.customer, e.prepay.quote);
    const account = await sub.readAccount(client, e.prepay.account.id, 'USDC', e.simulated_purchase.binding.customer);
    const balance = await card.cardBalances(client, e.simulated_purchase.binding);
    if (!account.cancelled || account.balance.value !== '0' || balance.usdc !== 0n) throw Error('Refund/cleanup not confirmed');
    const parentUsed = e.transactions.filter(t => t.status !== 'not_submitted').reduce((s,t) => s+BigInt(t.gas_mist)+BigInt(t.funding_mist),0n);
    const childUsed = e.child_transactions.filter(t=>t.status !== 'not_submitted').reduce((s,t)=>s+BigInt(t.gas_mist),0n);
    if (parentUsed.toString() !== String(e.allowance_used_mist) || childUsed.toString() !== String(e.child_used_mist) || parentUsed > 100000000n || childUsed > BigInt(e.child_funding_mist)) throw Error('Allowance mismatch');
    e.independent_grpc = { checks, transfers, final_account: account, final_wallet_usdc: balance.usdc.toString(), final_wallet_sui: balance.sui.toString(), parent_allowance_used_mist: parentUsed.toString(), child_used_mist: childUsed.toString() };
    fs.writeFileSync(offerId, JSON.stringify(e,null,2)+'\n');
    console.log('PASS: independent Testnet effects/gas, partial/full recipient USDC transfers, typed prepay, cancelled/empty account and refunded wallet');
    return;
  }
  for await (const line of { [Symbol.asyncIterator]: () => input }) {
    const q = JSON.parse(line);
    if (q.action === 'start') {
      const obj = await client.getObject({ objectId: offerId, include: { content: true } });
      const offer = load('lib/fleet-protocol.ts').subscription_OfferBcs.parse(obj.object.content);
      quote = { offerId, symbol: 'USDC', usdPerCoin: 1, observedAt: Number(offer.published_at_ms), source: 'Test-only USDC denomination: 1 USD/USDC, no market-price claim' };
      binding = await card.cardBinding(client, quote, recipient, q.customer);
      const before = await card.cardBalances(client, binding);
      purchase = q.purchase ?? card.startCardPurchase(binding, before.usdc);
      if (card.bindingKey(purchase.binding) !== card.bindingKey(binding)) throw Error('Restored purchase binding mismatch');
      console.log(JSON.stringify({ binding, purchase, state: card.cardStatus(purchase, binding, before.usdc, before.sui) }, (_, v) => typeof v === 'bigint' ? v.toString() : v));
    } else if (q.action === 'observe') {
      const balance = await card.cardBalances(client, binding);
      console.log(JSON.stringify({ ...balance, ...card.cardStatus(purchase, binding, balance.usdc, balance.sui) }, (_, v) => typeof v === 'bigint' ? v.toString() : v));
    } else if (q.action === 'transfer') {
      const tx = new Transaction();
      tx.transferObjects([tx.add(coinWithBalance({ type: sub.coin('USDC').coin_type, balance: BigInt(q.amount) }))], recipient);
      tx.setSender(q.sender); tx.setGasBudget(10000000);
      console.log(JSON.stringify({ tx: toBase64(await tx.build({ client })) }));
    } else if (q.action === 'prepay') {
      const balance = await card.cardBalances(client, binding);
      if (card.cardStatus(purchase, binding, balance.usdc, balance.sui).state !== 'ready') throw Error('Purchase not ready');
      const tx = await sub.preparePrepay(client, quote, binding.customer, recipient);
      tx.setSender(recipient); tx.setGasBudget(20000000);
      console.log(JSON.stringify({ tx: toBase64(await tx.build({ client })) }));
    } else if (q.action === 'verify') {
      const current = await sub.verifyPlan(client, q.digest, binding.customer, quote);
      console.log(JSON.stringify({ account: current.account, quote, digest: q.digest }));
    } else throw Error('Unknown bridge action');
  }
})().catch(e => { console.error(e.message); process.exitCode = 1; });
