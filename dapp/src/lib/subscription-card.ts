import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { normalizeId, requireManifest, verifyDeployment } from './deployment-manifest';
import { coin, readOffer, type Quote } from './subscription';
import { PURCHASE_TIMEOUT_MS, type PurchaseStorage } from './onramp';

// No production provider is selected. The SF-083 network-budget app ID is NOT
// authorization to enable subscription purchases or evidence of USDC coverage.
export const CARD_DISABLED = 'Card checkout awaits your provider choice and partner signup. No production card payment is enabled.';
export interface CardBinding {
  chain: string; package: string; wallet: string; customer: string;
  coinType: string; quote: Quote; total: string; expiresAt: number;
}
export interface CardPurchase {
  version: 1; binding: CardBinding; startBalance: string; startedAt: number;
  abandoned: boolean; submission?: { state: 'approving' | 'submitted' | 'confirmed'; digest?: string };
}
export function bindingKey(b: CardBinding): string {
  return `suifleet:subscription-card:v1:${JSON.stringify(b)}`;
}
function valid(b: CardBinding) {
  if (!/^[0-9a-f]{8}$/.test(b.chain) || b.quote.symbol !== 'USDC' || !/^[1-9][0-9]{0,19}$/.test(b.total) || BigInt(b.total) > BigInt('18446744073709551615') || !Number.isSafeInteger(b.expiresAt)) throw Error('Invalid card quote binding');
  for (const id of [b.package, b.wallet, b.customer, b.quote.offerId]) if (normalizeId(id) !== id) throw Error('Canonical card binding required');
  if (!b.coinType || !Number.isFinite(b.quote.usdPerCoin) || b.quote.usdPerCoin <= 0 || !Number.isSafeInteger(b.quote.observedAt) || !b.quote.source) throw Error('Invalid card quote');
}
export function startCardPurchase(binding: CardBinding, balance: bigint, now = Date.now()): CardPurchase {
  valid(binding);
  if (balance < BigInt('0') || !Number.isSafeInteger(now) || now >= binding.expiresAt) throw Error('Expired quote or invalid balance');
  return { version: 1, binding, startBalance: balance.toString(), startedAt: now, abandoned: false };
}
export function saveCardPurchase(storage: PurchaseStorage, p: CardPurchase) {
  storage.setItem(bindingKey(p.binding), JSON.stringify(p));
}
export function loadCardPurchase(storage: PurchaseStorage, binding: CardBinding): CardPurchase | null {
  valid(binding);
  const raw = storage.getItem(bindingKey(binding));
  if (!raw) return null;
  try {
    const p: CardPurchase = JSON.parse(raw);
    if (p.version !== 1 || bindingKey(p.binding) !== bindingKey(binding) || !/^[0-9]{1,20}$/.test(p.startBalance) || !Number.isSafeInteger(p.startedAt) || typeof p.abandoned !== 'boolean' ||
      (p.submission && (!['approving', 'submitted', 'confirmed'].includes(p.submission.state) || (p.submission.state !== 'approving' && !p.submission.digest)))) throw Error('Invalid saved purchase');
    return p;
  } catch { throw Error('Saved purchase is unreadable. Reconcile wallet funds and submitted transactions before starting again.'); }
}
export type CardState = 'waiting' | 'partial' | 'ready' | 'late' | 'abandoned' | 'expired' | 'gas-needed' | 'approving' | 'submitted' | 'confirmed';
export function cardStatus(p: CardPurchase, binding: CardBinding, balance: bigint, sui: bigint, now = Date.now()): { state: CardState; received: bigint; missing: bigint } {
  if (bindingKey(p.binding) !== bindingKey(binding)) throw Error('Wallet, network, customer or quote changed; select the original purchase context');
  const delta = balance - BigInt(p.startBalance), received = delta > BigInt('0') ? delta : BigInt('0');
  const needed = BigInt(binding.total), missing = balance < needed ? needed - balance : BigInt('0');
  let state: CardState;
  if (p.submission) state = p.submission.state;
  else if (now >= binding.expiresAt) state = 'expired';
  else if (p.abandoned) state = 'abandoned';
  else if (!received) state = now - p.startedAt >= PURCHASE_TIMEOUT_MS ? 'late' : 'waiting';
  else if (missing) state = 'partial';
  else state = sui > BigInt('0') ? 'ready' : 'gas-needed';
  return { state, received, missing };
}
export function cardTestMode(network = requireManifest().network, chain = requireManifest().chain_identifier, flag = process.env.NEXT_PUBLIC_SUBSCRIPTION_CARD_TEST): boolean {
  return flag === 'true' && network === 'testnet' && chain === '4c78adac';
}
export async function cardBinding(client: SuiGrpcClient, quote: Quote, wallet: string, customer: string): Promise<CardBinding> {
  if (quote.symbol !== 'USDC') throw Error('Card purchases require a USDC offer');
  const m = requireManifest(), offer = await readOffer(client, quote);
  const b = { chain: m.chain_identifier, package: m.call_package_id, wallet: normalizeId(wallet), customer: normalizeId(customer), coinType: coin('USDC').coin_type, quote, total: offer.total.toString(), expiresAt: Number(offer.offer.expires_at_ms) };
  valid(b); return b;
}
export async function cardBalances(client: SuiGrpcClient, b: CardBinding) {
  const m = requireManifest();
  if (b.chain !== m.chain_identifier || b.package !== m.call_package_id || b.coinType !== coin('USDC').coin_type) throw Error('Purchase deployment changed');
  await verifyDeployment(client);
  const [usdc, sui] = await Promise.all([client.getBalance({ owner: b.wallet, coinType: b.coinType }), client.getBalance({ owner: b.wallet, coinType: '0x2::sui::SUI' })]);
  return { usdc: BigInt(usdc.balance.balance), sui: BigInt(sui.balance.balance) };
}
