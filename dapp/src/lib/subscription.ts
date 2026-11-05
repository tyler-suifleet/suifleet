import { bcs } from '@mysten/sui/bcs';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import { normalizeStructTag } from '@mysten/sui/utils';
import { moveType, normalizeId, requireManifest, subscriptionCoin, treasuryPayee, verifyDeployment } from './deployment-manifest';
import { subscription_OfferBcs, subscription_AccountBcs, subscription_DepositedBcs, subscription_CollectedBcs, subscription_RefundedBcs, subscription_PlanChangedBcs } from './fleet-protocol';
import { readCustomerEntitlement } from './customer-entitlements';
import { incrementalEvents } from './event-history';
import { basePrice, loadPriceTable } from './fleet-pricing';

export type Symbol = 'USDC' | 'SUI';
export type Offer = ReturnType<typeof subscription_OfferBcs.parse>;
export type Account = ReturnType<typeof subscription_AccountBcs.parse>;
export interface Quote { offerId: string; symbol: Symbol; usdPerCoin: number; observedAt: number; source: string }
export interface Receipt { kind: string; digest: string; amount: string; accountId: string; checkpoint: string; eventIndex: number; timestampMs: number | null; throughMs: string | null }
const U64 = BigInt('18446744073709551615');
export function units(value: string): bigint {
  if (value.length > 20 || !/^[1-9][0-9]*$/.test(value) || BigInt(value) > U64) throw Error('Enter a positive whole amount in coin base units');
  return BigInt(value);
}
export function quotes(raw = process.env.NEXT_PUBLIC_SUBSCRIPTION_QUOTES): Quote[] {
  if (!raw) return [];
  const list: Quote[] = JSON.parse(raw);
  if (!Array.isArray(list) || list.length > 100) throw Error('Invalid subscription quote list');
  for (const q of list) {
    normalizeId(q.offerId);
    if (!['USDC', 'SUI'].includes(q.symbol) || !Number.isFinite(q.usdPerCoin) || q.usdPerCoin <= 0 ||
      !Number.isSafeInteger(q.observedAt) || q.observedAt <= 0 || !q.source?.trim()) throw Error('Invalid subscription conversion source/time');
  }
  if (new Set(list.map(q => normalizeId(q.offerId))).size !== list.length) throw Error('Duplicate subscription offer');
  return list;
}
export function coin(symbol: Symbol) {
  if (symbol !== 'USDC' && symbol !== 'SUI') throw Error('Unsupported subscription coin');
  const descriptor = requireManifest().subscription_coins?.[symbol];
  if (!descriptor) throw Error(`${symbol} subscriptions are not configured`);
  subscriptionCoin(symbol, descriptor.coin_type, treasuryPayee());
  return descriptor;
}
export function planUsd(devices: number, annual: boolean) {
  const { table, error } = loadPriceTable();
  if (!table) throw Error(error);
  if (!Number.isSafeInteger(devices) || devices < 1 || devices > table.base.tiers.at(-1)!.upToDevices) throw Error('Custom capacity requires a reviewed quote');
  const monthly = Math.max(table.base.monthlyMinimumUsd, basePrice(table, devices).usd);
  return monthly * (annual ? 12 * (1 - table.base.annualPrepayDiscountPercent / 100) : 1);
}
export function validateOffer(o: Offer, q: Quote, now = Date.now()) {
  const m = requireManifest(), c = coin(q.symbol);
  if (normalizeId(o.id) !== normalizeId(q.offerId) || normalizeId(o.registry_id) !== normalizeId(m.shared_objects.registry.id) || o.treasury !== treasuryPayee()) throw Error('Offer registry or treasury mismatch');
  const annual = o.periods === '12';
  if ((!annual && o.periods !== '1') || o.period_ms !== (annual ? '2628000000' : '2592000000') || BigInt(o.fee) <= BigInt(0)) throw Error('Unsupported subscription term');
  if (now > Number(o.expires_at_ms) || q.observedAt > now || q.observedAt > Number(o.published_at_ms) || q.observedAt < Number(o.published_at_ms) - 86400000) throw Error('Expired offer or invalid dated conversion');
  const usd = planUsd(Number(o.device_limit), annual);
  const total = BigInt(o.fee) * BigInt(o.periods);
  const monthlyCents = Math.round(planUsd(Number(o.device_limit), false) * 100);
  if (!Number.isSafeInteger(monthlyCents)) throw Error('Price exceeds safe quote range');
  // Decimal rational arithmetic avoids float underpayment and follows Move's
  // per-period rounding, including annual discount AFTER coin conversion.
  const [mantissa, exponent = '0'] = q.usdPerCoin.toString().toLowerCase().split('e');
  const [whole, fraction = ''] = mantissa.split('.');
  const power = Number(exponent) - fraction.length;
  const numerator = BigInt(whole + fraction) * (power > 0 ? BigInt(10) ** BigInt(power) : BigInt(1));
  const denominator = power < 0 ? BigInt(10) ** BigInt(-power) : BigInt(1);
  const top = BigInt(monthlyCents) * BigInt(10) ** BigInt(c.decimals) * denominator;
  const bottom = BigInt(100) * numerator;
  const monthlyFee = (top + bottom - BigInt(1)) / bottom;
  if (annual && loadPriceTable().table!.base.annualPrepayDiscountPercent !== 10) throw Error('Contract supports a 10% annual discount only');
  const fee = annual ? (monthlyFee * BigInt(9) + BigInt(9)) / BigInt(10) : monthlyFee;
  if (BigInt(o.fee) !== fee || total > U64) throw Error('Offer price does not match canonical pricing and recorded conversion');
  return { usd, total, annual, descriptor: c };
}
export async function readOffer(client: SuiGrpcClient, q: Quote) {
  await verifyDeployment(client);
  const { object } = await client.getObject({ objectId: normalizeId(q.offerId), include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(`${moveType('subscription::Offer')}<${coin(q.symbol).coin_type}>`) || object.owner.$kind !== 'Immutable') throw Error('Expected an immutable offer in the pinned coin');
  const offer = subscription_OfferBcs.parse(object.content);
  return { offer, ...validateOffer(offer, q) };
}
export async function readAccount(client: SuiGrpcClient, id: string, symbol: Symbol, customerId?: string) {
  await verifyDeployment(client);
  const { object } = await client.getObject({ objectId: normalizeId(id), include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(`${moveType('subscription::Account')}<${coin(symbol).coin_type}>`) || object.owner.$kind !== 'Shared') throw Error('Expected a shared subscription account in the pinned coin');
  const account = subscription_AccountBcs.parse(object.content);
  if (normalizeId(account.id) !== normalizeId(id) || account.treasury !== treasuryPayee() || normalizeId(account.registry_id) !== normalizeId(requireManifest().shared_objects.registry.id) || (customerId && normalizeId(account.customer_id) !== normalizeId(customerId))) throw Error('Subscription account binding mismatch');
  return account;
}
export async function customerAccount(client: SuiGrpcClient, customerId: string) {
  await readCustomerEntitlement(client, customerId);
  let cursor: string | null | undefined;
  do {
    const page = await client.listDynamicFields({ parentId: customerId, cursor, limit: 100, include: { value: true } });
    for (const f of page.dynamicFields) if (normalizeStructTag(f.name.type) === normalizeStructTag(moveType('customer::SubscriptionKey'))) {
      const id = bcs.Address.parse(f.value.bcs);
      // Read object type first, so a transport failure never masquerades as a
      // missing account or an invitation to open a second one.
      const { object } = await client.getObject({ objectId: id });
      for (const symbol of ['USDC', 'SUI'] as const) {
        const c = requireManifest().subscription_coins?.[symbol];
        if (c && normalizeStructTag(object.type) === normalizeStructTag(`${moveType('subscription::Account')}<${c.coin_type}>`)) return { symbol, account: await readAccount(client, id, symbol, customerId) };
      }
      throw Error('Subscription uses an unsupported coin');
    }
    if (page.hasNextPage && (!page.cursor || page.cursor === cursor)) throw Error('Incomplete subscription binding scan');
    cursor = page.hasNextPage ? page.cursor : undefined;
  } while (cursor);
  return null;
}
export function warnings(a: Account, now = Date.now()) {
  const result: string[] = [];
  if (a.cancelled) result.push('Subscription cancelled; no further periods will be collected.');
  else {
    if (BigInt(a.balance.value) < BigInt(a.fee) * BigInt(a.remaining)) result.push('Low subscription balance: remaining periods are not fully funded.');
    if (Number(a.term_end_ms) - now <= 7 * 86400000) result.push(Number(a.term_end_ms) <= now ? 'Subscription term expired. Choose a renewal offer.' : 'Subscription term ends within seven days. Review a renewal offer before expiry.');
    if (BigInt(a.remaining) > BigInt(0) && now >= Number(a.next_due_ms)) result.push('A prepaid payment is due. Apply it here to activate or extend device capacity using your existing subscription balance; approval costs a network fee.');
  }
  return result;
}
export async function preparePrepay(client: SuiGrpcClient, q: Quote, customerId: string, sender: string) {
  const { offer, total } = await readOffer(client, q);
  const customer = await readCustomerEntitlement(client, customerId);
  if (!customer.credentials.contents.some(c => normalizeId(c.key) === normalizeId(sender) && c.value)) throw Error('Only a current customer administrator can choose or renew a plan');
  if (BigInt(offer.device_limit) < BigInt(customer.active_devices)) throw Error('Plan capacity is below active device count');
  const existing = await customerAccount(client, customerId);
  const tx = new Transaction(), m = requireManifest();
  if (existing && existing.symbol !== q.symbol) throw Error('A subscription keeps its original coin; changing coins requires migration');
  if (existing && !existing.account.cancelled && (Date.now() < Number(existing.account.term_end_ms) || existing.account.remaining !== '0')) throw Error('The current term must end and all periods must be collected before renewal');
  if (existing && Date.now() < Number(offer.published_at_ms) + 30 * 86400000) throw Error('Renewal quote must have been published at least 30 days ago');
  const amount = existing ? (total > BigInt(existing.account.balance.value) ? total - BigInt(existing.account.balance.value) : BigInt(0)) : total;
  const { balance } = await client.getBalance({ owner: sender, coinType: coin(q.symbol).coin_type });
  if (BigInt(balance.balance) < amount) throw Error(`Insufficient ${q.symbol} funds; transaction gas also requires SUI`);
  const payment = () => tx.add(coinWithBalance({ type: coin(q.symbol).coin_type, balance: amount }));
  if (existing) {
    if (amount > BigInt(0)) tx.moveCall({ target: `${m.call_package_id}::subscription::deposit`, typeArguments: [coin(q.symbol).coin_type], arguments: [tx.object(existing.account.id), payment()] });
    tx.moveCall({ target: `${m.call_package_id}::subscription::renew`, typeArguments: [coin(q.symbol).coin_type], arguments: [tx.object(existing.account.id), tx.object(customerId), tx.object(offer.id), tx.object('0x6')] });
  } else tx.moveCall({ target: `${m.call_package_id}::subscription::open`, typeArguments: [coin(q.symbol).coin_type], arguments: [tx.object(customerId), tx.object(m.shared_objects.registry.id), tx.object(offer.id), payment(), tx.object('0x6')] });
  return tx;
}
export function depositTx(account: Account, symbol: Symbol, amount: string) {
  const tx = new Transaction();
  if (account.treasury !== treasuryPayee()) throw Error('Treasury mismatch');
  tx.moveCall({ target: `${requireManifest().call_package_id}::subscription::deposit`, typeArguments: [coin(symbol).coin_type], arguments: [tx.object(account.id), tx.add(coinWithBalance({ type: coin(symbol).coin_type, balance: units(amount) }))] });
  return tx;
}
export function collectTx(account: Account, symbol: Symbol) {
  const tx = new Transaction();
  tx.moveCall({ target: `${requireManifest().call_package_id}::subscription::collect`, typeArguments: [coin(symbol).coin_type], arguments: [tx.object(account.id), tx.object(account.customer_id), tx.object('0x6')] });
  return tx;
}
export interface Payment { chain: string; package: string; account: string; customer: string; symbol: Symbol; amount: string }
export function validatePayment(p: Payment) {
  const m = requireManifest();
  if (p.chain !== m.chain_identifier || normalizeId(p.package) !== normalizeId(m.call_package_id)) throw Error('Payment link network or package mismatch');
  normalizeId(p.account); normalizeId(p.customer); coin(p.symbol); units(p.amount);
  return p;
}
export function paymentUri(origin: string, p: Payment) {
  validatePayment(p);
  const url = new URL('/billing/', origin);
  url.hash = new URLSearchParams(p as unknown as Record<string, string>).toString();
  return url.toString();
}
export function parsePayment(hash: string): Payment | undefined {
  if (!hash || hash === '#') return;
  const p = new URLSearchParams(hash.replace(/^#/, ''));
  if (['chain', 'package', 'account', 'customer', 'symbol', 'amount'].some(k => p.getAll(k).length !== 1) || [...p.keys()].length !== 6) throw Error('Malformed payment link');
  return validatePayment(Object.fromEntries(p) as unknown as Payment);
}
export async function verifyDeposit(client: SuiGrpcClient, digest: string, p: Payment) {
  validatePayment(p);
  await readAccount(client, p.account, p.symbol, p.customer);
  const result = await client.waitForTransaction({ digest, include: { effects: true, events: true } });
  if (result.FailedTransaction || !result.Transaction.status.success || result.Transaction.digest !== digest) throw Error('Payment execution failed');
  const deposits = (result.Transaction.events ?? []).filter(e => normalizeStructTag(e.eventType) === normalizeStructTag(moveType('subscription::Deposited'))).map(e => subscription_DepositedBcs.parse(e.bcs));
  if (!deposits.some(e => normalizeId(e.account_id) === normalizeId(p.account) && e.amount === p.amount)) throw Error('Transaction does not contain this subscription deposit');
  return result.Transaction.digest;
}
export async function verifyPlan(client: SuiGrpcClient, digest: string, customerId: string, q: Quote) {
  await verifyDeployment(client);
  const result = await client.waitForTransaction({ digest, include: { effects: true, events: true } });
  if (result.FailedTransaction || !result.Transaction.status.success || result.Transaction.digest !== digest) throw Error('Plan payment execution failed');
  const current = await customerAccount(client, customerId);
  if (!current || current.symbol !== q.symbol) throw Error('Subscription binding was not confirmed');
  const events = (result.Transaction.events ?? []).filter(e => normalizeStructTag(e.eventType) === normalizeStructTag(moveType('subscription::PlanChanged'))).map(e => subscription_PlanChangedBcs.parse(e.bcs));
  if (!events.some(e => normalizeId(e.account_id) === normalizeId(current.account.id) && normalizeId(e.offer_id) === normalizeId(q.offerId) && e.fee === current.account.fee && e.tier === current.account.tier)) throw Error('Transaction does not confirm this subscription plan');
  return current;
}
export async function history(client: SuiGrpcClient, accountId: string): Promise<Receipt[]> {
  await verifyDeployment(client);
  const result: Receipt[] = [];
  const dates = new Map<string, number | null>();
  for (const [kind, codec] of [['Deposited', subscription_DepositedBcs], ['Collected', subscription_CollectedBcs], ['Refunded', subscription_RefundedBcs]] as const) {
    for (const e of await incrementalEvents(client, moveType(`subscription::${kind}`))) {
      const value = codec.parse(e.bcs);
      if (normalizeId(value.account_id) !== normalizeId(accountId)) continue;
      if (!dates.has(e.transactionDigest)) {
        const tx = await client.getTransaction({ digest: e.transactionDigest });
        if (tx.$kind !== 'Transaction') throw Error('Receipt transaction unavailable');
        dates.set(e.transactionDigest, tx.Transaction.timestampMs);
      }
      if (e.checkpoint === null) throw Error('Receipt checkpoint unavailable');
      result.push({ kind, digest: e.transactionDigest, amount: value.amount, accountId,
        checkpoint: e.checkpoint, eventIndex: e.eventIndex, timestampMs: dates.get(e.transactionDigest)!,
        throughMs: kind === 'Collected' ? subscription_CollectedBcs.parse(e.bcs).through_ms : null });
    }
  }
  return result.sort((a, b) => {
    const checkpoint = BigInt(a.checkpoint) - BigInt(b.checkpoint);
    return checkpoint < BigInt(0) ? -1 : checkpoint > BigInt(0) ? 1 :
      a.eventIndex - b.eventIndex || a.digest.localeCompare(b.digest);
  });
}
/** UTC avoids ambiguous locale-dependent accounting dates. */
export function receiptDate(milliseconds: number | string | null) {
  return milliseconds === null ? 'Unavailable' : new Date(Number(milliseconds)).toISOString();
}
const csv = (value: string) => '"' + (/^(?:\s*[=+\-@]|[\t\r\n])/.test(value) ? "'" : '') + value.replaceAll('"', '""') + '"';
export function receiptsCsv(rows: Receipt[], company: string, address: string, taxId: string, symbol: Symbol) {
  const m = requireManifest();
  const header = ['Company', 'Company address', 'Tax ID', 'Chain', 'Treasury', 'Account', 'Kind', 'Coin type', 'Amount (base units)', 'Date (UTC)', 'Checkpoint', 'Event index', 'Collected through (UTC)', 'Digest'];
  return '\uFEFF' + [header, ...rows.map(r => [company, address, taxId, m.chain_identifier, treasuryPayee(), r.accountId, r.kind, coin(symbol).coin_type, r.amount, receiptDate(r.timestampMs), r.checkpoint, String(r.eventIndex), r.throughMs === null ? '' : receiptDate(r.throughMs), r.digest])].map(row => row.map(csv).join(',')).join('\r\n');
}

/** Optional display only; lookup never supplies an address or changes a PTB. */
export async function treasuryLabel(client: SuiGrpcClient) {
  await verifyDeployment(client);
  const address = treasuryPayee();
  const { data: { name } } = await client.defaultNameServiceName({ address });
  return name === 'suifleet.sui' ? name : undefined;
}
