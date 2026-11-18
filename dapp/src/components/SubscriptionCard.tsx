'use client';
import { useEffect, useRef, useState } from 'react';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { useQuery } from '@tanstack/react-query';
import { formatToken } from '@/lib/network-costs';
import { coin, type Quote } from '@/lib/subscription';
import { CARD_DISABLED, bindingKey, cardBinding, cardBalances, cardStatus, cardTestMode, loadCardPurchase, saveCardPurchase, startCardPurchase, type CardPurchase, type CardBinding } from '@/lib/subscription-card';

/** Only observes chain balances. No provider callback can authorize a prepay. */
export function SubscriptionCard({ client, quote, wallet, customer, disabled, prepay }: {
  client: SuiGrpcClient; quote: Quote; wallet: string; customer: string; disabled: boolean;
  prepay: (onSubmitted: (digest: string) => void) => Promise<boolean>;
}) {
  const binding = useQuery({ queryKey: ['card-binding', quote, wallet, customer], queryFn: () => cardBinding(client, quote, wallet, customer), retry: false });
  if (!binding.data) return <p role="status">{binding.error ? binding.error.message : 'Verifying card purchase context…'}</p>;
  return <Purchase key={bindingKey(binding.data)} client={client} binding={binding.data} disabled={disabled} prepay={prepay} />;
}
function Purchase({ client, binding, disabled, prepay }: { client: SuiGrpcClient; binding: CardBinding; disabled: boolean; prepay: (submitted: (digest: string) => void) => Promise<boolean> }) {
  const [purchase, setPurchase] = useState<CardPurchase | null>(null), [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false), [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 10000); return () => clearInterval(timer); }, []);
  const alive = useRef(true), running = useRef(false);
  useEffect(() => { alive.current = true; try { setPurchase(loadCardPurchase(localStorage, binding)); } catch (e) { setError(String(e)); } setLoaded(true); return () => { alive.current = false; }; }, [binding]);
  // Polls the verified chain, including abandoned/late attempts. The persisted
  // record survives closing the browser, changing wallet and quote expiry.
  const balances = useQuery({ queryKey: ['card-balances', bindingKey(binding)], queryFn: () => cardBalances(client, binding), retry: false, refetchInterval: 10000 });
  const test = cardTestMode();
  const status = purchase && balances.data && !balances.isError ? cardStatus(purchase, binding, balances.data.usdc, balances.data.sui, now) : undefined;
  function persist(p: CardPurchase) { saveCardPurchase(localStorage, p); if (alive.current) setPurchase(p); }
  async function begin() {
    if (running.current || purchase || !test || !loaded || error) return;
    running.current = true;
    try {
      const saved = loadCardPurchase(localStorage, binding);
      if (saved) { setPurchase(saved); return; }
      const fresh = await cardBalances(client, binding);
      if (!alive.current) return;
      persist(startCardPurchase(binding, fresh.usdc));
    } catch (e) { if (alive.current) setError(String(e)); } finally { running.current = false; }
  }
  async function approve() {
    if (running.current || !purchase || disabled) return;
    running.current = true;
    try {
      const saved = loadCardPurchase(localStorage, binding);
      if (!saved || saved.submission) throw Error('A payment attempt already needs reconciliation; do not submit twice.');
      const fresh = await cardBalances(client, binding);
      if (!alive.current || cardStatus(saved, binding, fresh.usdc, fresh.sui).state !== 'ready') throw Error('Purchase is not ready. Refresh and review funds, gas and quote.');
      const latest = loadCardPurchase(localStorage, binding);
      if (!latest || latest.submission || latest.abandoned) throw Error('Purchase changed in another tab; refresh before approval.');
      let pending: CardPurchase = { ...latest, submission: { state: 'approving' } };
      // Save BEFORE requesting approval. A reload or uncertain submission must
      // never silently reopen a second payment attempt.
      persist(pending);
      const success = await prepay(digest => { pending = { ...pending, submission: { state: 'submitted', digest } }; persist(pending); });
      if (success && pending.submission?.digest) persist({ ...pending, submission: { state: 'confirmed', digest: pending.submission.digest } });
    } catch (e) { if (alive.current) setError(String(e)); } finally { running.current = false; }
  }
  return <div className="space-y-2 rounded border p-3">
    <h3 className="font-semibold">Pay by card</h3><p>{CARD_DISABLED}</p>
    <button disabled>Production card checkout unavailable</button>
    <p>USDC funds the subscription. SUI for transaction gas must be funded separately. Provider fees, exchange rates, KYC and regional availability will be shown by the chosen provider; no final card price is available here.</p>
    {test && <><p className="font-semibold">TESTNET SIMULATED PURCHASE — no card charge or production USDC</p><p>Start tracking, then send testnet USDC from a separate test wallet to the address below. This real chain transfer simulates the hosted purchase. No balance or provider success is fabricated.</p><p className="break-all">Recipient: {binding.wallet}<br />Coin: {binding.coinType}<br />Target wallet funds: {formatToken(BigInt(binding.total), coin('USDC').decimals)} USDC, plus separate SUI gas.</p><button disabled={disabled || !loaded || !!error || !!purchase} onClick={() => void begin()}>Start testnet simulated purchase</button></>}
    {purchase && <><p role="status">Purchase: {status?.state ?? 'checking chain'}{status && <> · Net USDC balance increase {formatToken(status.received, coin('USDC').decimals)} · Still needed {formatToken(status.missing, coin('USDC').decimals)} USDC</>}</p><p>Balance changes establish available funds, not which provider sent them. Spending from this wallet reduces the observed increase. Partial, late and abandoned funds stay in your wallet. Tracking never charges or prepays automatically. Expired quotes need a fresh reviewed offer; existing funds can be used by normal wallet prepay.</p>
      {purchase.submission ? <p>Payment approval was started. Reconcile the displayed digest and subscription before any retry; this saved attempt cannot submit again. {purchase.submission.digest}</p> : <><button disabled={disabled || status?.state !== 'ready'} onClick={() => void approve()}>Review and approve subscription prepay</button><button disabled={disabled} onClick={() => { try { persist({ ...purchase, abandoned: !purchase.abandoned }); } catch (e) { setError(String(e)); } }}>{purchase.abandoned ? 'Resume tracking and review' : 'Abandon checkout; keep wallet funds'}</button></>}
    </>}
    {balances.error && <p role="alert">Chain balance unavailable: {balances.error.message}</p>}{error && <p role="alert">{error}</p>}
  </div>;
}
