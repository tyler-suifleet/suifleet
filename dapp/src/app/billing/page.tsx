"use client";
import { useEffect, useRef, useState } from 'react';
import type React from 'react';
import { useCurrentClient } from '@mysten/dapp-kit-react';
import { useQueries, useQuery } from '@tanstack/react-query';
import QRCode from 'qrcode';
import { SubscriptionCard } from '@/components/SubscriptionCard';
import { useCurrentAccount } from '@/hooks/useCurrentAccount';
import { useFleetSelection } from '@/hooks/useFleetSelection';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { PageHeader } from '@/components/ui';
import { formatToken, parseToken } from '@/lib/network-costs';
import { loadPriceTable } from '@/lib/fleet-pricing';
import { requireManifest, treasuryPayee } from '@/lib/deployment-manifest';
import { Transaction } from '@mysten/sui/transactions';
import { quotes, readOffer, customerAccount, planUsd, preparePrepay, treasuryLabel, coin, paymentUri, parsePayment, readAccount, verifyDeposit, verifyPlan, receiptsCsv, receiptDate, history, warnings, collectTx, type Payment, type Quote } from '@/lib/subscription';

const button = 'rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium hover:bg-slate-50 disabled:opacity-50';
const primary = 'rounded-lg bg-[#4DA2FF] px-4 py-2 text-sm font-medium text-white hover:bg-[#2e8ed4] disabled:opacity-50';
const card = 'rounded-2xl border border-slate-200 bg-white p-6 space-y-5 shadow-sm';
const input = 'mt-1 block w-full rounded border border-slate-300 p-2';
export default function BillingPage() {
  return <div className="max-w-4xl space-y-6"><PageHeader title="Plans and billing" subtitle="Device slots for your organization, and how you pay for them." /><Billing /></div>;
}
function Billing() {
  const client = useCurrentClient(), wallet = useCurrentAccount(), selection = useFleetSelection(), mutation = useFleetTransaction();
  const [busy, setBusy] = useState(false);
  const [activeAction, setActiveAction] = useState('');
  const [selected, setSelected] = useState(''), [message, setMessage] = useState(''), [digest, setDigest] = useState('');
  const [linkPayment, setLinkPayment] = useState<Payment>(), [linkError, setLinkError] = useState('');
  const [share, setShare] = useState(''), [qr, setQr] = useState(''), [amount, setAmount] = useState('');
  const [company, setCompany] = useState(''), [address, setAddress] = useState(''), [taxId, setTaxId] = useState('');
  const context = `${selection.identity}:${wallet?.address ?? ''}:${selected}:${JSON.stringify(linkPayment)}`;
  const latest = useRef(context); latest.current = context;
  const inFlight = useRef(false);
  useEffect(() => {
    const load = () => { try { setLinkPayment(parsePayment(window.location.hash)); setLinkError(''); } catch (e) { setLinkError(String(e)); setLinkPayment(undefined); } };
    load(); window.addEventListener('hashchange', load); return () => window.removeEventListener('hashchange', load);
  }, []);
  useEffect(() => { setMessage(''); setShare(''); setQr(''); setDigest(''); setAmount(''); setCompany(''); setAddress(''); setTaxId(''); }, [selection.identity, wallet?.address]);
  const config = useQuery({ queryKey: ['subscription-quotes'], queryFn: async () => ({ quotes: quotes(), treasury: treasuryPayee() }), retry: false });
  const label = useQuery({ queryKey: ['subscription-treasury-label'], queryFn: () => treasuryLabel(client), enabled: !!config.data, retry: false });
  const choices = useQueries({ queries: (config.data?.quotes ?? []).map(q => ({ queryKey: ['subscription-offer', q.offerId], queryFn: () => readOffer(client, q), retry: false, refetchInterval: 60000 })) });
  const quote: Quote | undefined = config.data?.quotes.find(q => q.offerId === selected);
  const offer = useQuery({ queryKey: ['subscription-offer', selected], queryFn: () => readOffer(client, quote!), enabled: !!quote, retry: false, refetchInterval: 60000 });
  const customerId = selection.fleet?.customerId;
  const current = useQuery({ queryKey: ['subscription-account', customerId, selection.identity], queryFn: () => customerAccount(client, customerId!), enabled: !!customerId && !!config.data, retry: false, refetchInterval: 60000 });
  const target = useQuery({ queryKey: ['subscription-payment', linkPayment], queryFn: () => readAccount(client, linkPayment!.account, linkPayment!.symbol, linkPayment!.customer), enabled: !!linkPayment, retry: false, refetchInterval: 60000 });
  const receipts = useQuery({ queryKey: ['subscription-history', current.data?.account.id], queryFn: () => history(client, current.data!.account.id), enabled: !!current.data, retry: false, refetchInterval: 60000 });
  const price = loadPriceTable();
  async function run(actionName: string, action: () => Promise<Transaction> | Transaction, payment?: Payment, plan?: Quote, submitted?: (digest: string) => void, done?: string) {
    if (inFlight.current) return false;
    inFlight.current = true;
    setBusy(true);
    setActiveAction(actionName);
    setDigest('');
    const captured = context;
    setMessage('Checking payment details and network…');
    try {
      const tx = await action();
      if (latest.current !== captured) throw Error("Account or fleet changed; retry");
      const result = await mutation.mutateAsync({ transaction: tx, subscriptionDeposit: payment, onProgress: phase => {
        if (latest.current !== captured) return;
        setMessage(phase === 'validating' ? 'Checking payment details and network…' : phase === 'wallet' ? 'Approve this billing action in your wallet…' : 'Submitted; checking the network result. Do not pay again while confirmation is uncertain.');
      }, onSubmitted: d => { submitted?.(d); if (latest.current !== captured) return; setDigest(d); setMessage('Submitted; checking the network result. Do not pay again while confirmation is uncertain.'); } });
      if (plan && customerId) await verifyPlan(client, result.digest, customerId, plan);
      if (payment) await verifyDeposit(client, result.digest, payment);
      if (latest.current !== captured) return false;
      setDigest(result.digest); setMessage(done ?? (plan ? 'Payment confirmed.' : 'Confirmed.'));
      if (customerId) { await current.refetch(); selection.refresh(); } if (linkPayment) await target.refetch(); if (current.data) await receipts.refetch();
      return true;
    } catch (e) { if (latest.current === captured) setMessage(`${e instanceof Error ? e.message : String(e)}. A digest alone is not payment confirmation. Check a submitted digest before retrying.`); return false; } finally { inFlight.current = false; setBusy(false); }
  }
  // Keep the progress message on screen next to its button, including on phones.
  useEffect(() => { document.querySelector('[data-action-status]')?.scrollIntoView?.({ block: 'nearest' }); }, [message, digest, activeAction]);
  async function sharePayment() {
    setActiveAction('');
    const captured = context;
    try {
      const a = current.data!;
      await readAccount(client, a.account.id, a.symbol, customerId);
      const url = paymentUri(window.location.origin, { chain: requireManifest().chain_identifier, package: requireManifest().call_package_id, account: a.account.id, customer: a.account.customer_id, symbol: a.symbol, amount: parseToken(amount, coin(a.symbol).decimals).toString() });
      const image = await QRCode.toDataURL(url, { errorCorrectionLevel: 'M', width: 320 });
      if (latest.current === captured) { setShare(url); setQr(image); }
    } catch (e) { setMessage(String(e)); }
  }
  async function confirmLink() {
    setActiveAction('');
    const captured = context;
    setMessage('Checking the submitted transaction…');
    try { await verifyDeposit(client, digest, linkPayment!); if (latest.current === captured) { setMessage('Subscription deposit confirmed on chain.'); await target.refetch(); } }
    catch (e) { if (latest.current === captured) setMessage(String(e)); }
  }
  const actionStatus = (name: string) => activeAction === name && message ? <p data-action-status role="status" className="scroll-my-4 break-words text-sm" aria-live="polite">{message}{digest && <span className="block break-all">Submitted digest: {digest}</span>}</p> : null;
  const fleet = selection.fleet as (typeof selection.fleet & { activeDevices?: bigint; deviceLimit?: bigint }) | undefined;
  const plans = (config.data?.quotes ?? []).map((q, i) => ({ q, choice: choices[i] }));
  const sub = current.data;
  const due = !!sub && !sub.account.cancelled && sub.account.remaining !== '0' && Date.now() >= Number(sub.account.next_due_ms);
  const dateOnly = (ms: string) => new Date(Number(ms)).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  const isDue = (a: NonNullable<typeof sub>) => !a.account.cancelled && a.account.remaining !== '0' && Date.now() >= Number(a.account.next_due_ms);
  const activeLimit = fleet?.deviceLimit, activeCount = fleet?.activeDevices;
  const status = !sub ? null : sub.account.cancelled ? { label: 'Cancelled', tone: 'bg-slate-100 text-slate-700' }
    : due ? { label: 'Needs activation', tone: 'bg-amber-100 text-amber-800' }
    : Number(sub.account.term_end_ms) - Date.now() <= 7 * 86400000 ? { label: 'Ends soon', tone: 'bg-amber-100 text-amber-800' }
    : { label: 'Active', tone: 'bg-emerald-100 text-emerald-800' };
  const otherWarnings = sub ? warnings(sub.account).filter(w => !/ready to start/.test(w)) : [];
  async function buy(q: Quote, submitted?: (digest: string) => void) {
    const ok = await run('prepay', () => preparePrepay(client, q, customerId!, wallet!.address), undefined, q, submitted);
    if (!ok) return ok;
    const fresh = (await current.refetch()).data;
    // A first purchase creates the subscription account; starting its first period is a second, permissionless call.
    if (fresh && isDue(fresh)) return run('prepay', () => collectTx(fresh.account, fresh.symbol), undefined, undefined, undefined, 'Done. Your device slots are active.');
    return ok;
  }
  const tabs = 'flex cursor-pointer list-none items-center justify-between rounded-lg px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 [&::-webkit-details-marker]:hidden';
  const chevron = <svg aria-hidden viewBox="0 0 20 20" className="h-4 w-4 text-slate-400 transition group-open:rotate-90"><path fill="currentColor" d="M7.3 4.3a1 1 0 0 1 1.4 0l5 5a1 1 0 0 1 0 1.4l-5 5a1 1 0 0 1-1.4-1.4L11.6 10 7.3 5.7a1 1 0 0 1 0-1.4Z" /></svg>;
  const renewLater = !!sub && !sub.account.cancelled && Date.now() < Number(sub.account.term_end_ms);
  return <>

    {wallet && customerId && <section className={`${card} overflow-hidden !p-0`} aria-labelledby="plan-status">
      <div className="flex flex-wrap items-start justify-between gap-3 px-6 pt-6">
        <div>
          <h2 id="plan-status" className="text-sm font-medium text-slate-500">Your plan</h2>
          {current.isPending && <p className="mt-1 text-slate-500">Loading your subscription…</p>}
          {sub === null && <p className="mt-1 text-2xl font-semibold tracking-tight">No plan yet</p>}
          {sub && <p className="mt-1 text-2xl font-semibold tracking-tight">{sub.account.device_limit}-device plan</p>}
          {sub && <p className="text-sm text-slate-500">{sub.account.period_ms === '2628000000' ? 'Annual' : 'Monthly'} · paid in {sub.symbol}</p>}
        </div>
        {status && <span className={`rounded-full px-3 py-1 text-xs font-semibold ${status.tone}`}>{status.label}</span>}
      </div>
      {current.error && <p role="alert" className="px-6">Subscription status unavailable: {current.error.message}</p>}
      {activeLimit !== undefined && activeCount !== undefined && <div className="px-6">
        <div className="flex items-baseline justify-between text-sm"><span className="font-medium text-slate-700">Devices in use</span><span className="text-slate-600"><b className="text-slate-900">{activeCount.toString()}</b> of {activeLimit.toString()}{due && sub && BigInt(sub.account.device_limit) > activeLimit && <> · {sub.account.device_limit} after activation</>}</span></div>
        <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-label="Device slots in use" aria-valuemin={0} aria-valuemax={Number(activeLimit)} aria-valuenow={Number(activeCount)}>
          <div className="h-full rounded-full bg-[#4DA2FF]" style={{ width: `${activeLimit > BigInt(0) ? Math.min(100, Number(activeCount * BigInt(100) / activeLimit)) : 0}%` }} />
        </div>
      </div>}
      {sub === null && <p className="px-6 pb-6 text-sm text-slate-600">Choose a plan below to add device slots. A customer administrator pays for the first term.</p>}
      {sub && <>
        {due && <div className="mx-6 rounded-xl border border-amber-200 bg-amber-50 p-4">
          <p className="font-semibold text-amber-900">Activate your device slots</p>
          <p className="mt-1 text-sm text-amber-900/80">Your payment is in your plan balance. Starting the period uses that balance, so you aren’t charged again; approving costs only a small network fee.</p>
          <div className="mt-3 space-y-2"><button className={primary} disabled={busy || mutation.isPending} onClick={() => void run('collect', () => collectTx(sub.account, sub.symbol), undefined, undefined, undefined, 'Done. Your device slots are active.')}>Activate device slots</button>{actionStatus('collect')}</div>
        </div>}
        <dl className="mx-6 grid grid-cols-1 gap-px overflow-hidden rounded-xl border bg-slate-200 sm:grid-cols-3">
          <Stat label={due ? 'Current period' : 'Paid through'} value={due ? 'Not started' : dateOnly(sub.account.next_due_ms)} />
          <Stat label="Plan term ends" value={dateOnly(sub.account.term_end_ms)} />
          <Stat label="Plan balance" value={`${formatToken(BigInt(sub.account.balance.value), coin(sub.symbol).decimals)} ${sub.symbol}`} />
        </dl>
        {otherWarnings.map(w => <p role="status" key={w} className="mx-6 text-sm text-amber-800">{w}</p>)}
        <div className="flex flex-wrap gap-1 border-t bg-slate-50/60 px-4 py-2">
          <details className="group w-full"><summary className={tabs}>Ask another wallet to pay{chevron}</summary>
            <div className="space-y-2 px-3 pb-3 pt-1"><label className="block text-sm">Amount in {sub.symbol}<input className={input} inputMode="decimal" value={amount} onChange={e => { setAmount(e.target.value); setShare(''); setQr(''); }} /></label>
            <button className={button} onClick={() => void sharePayment()}>Create payment link and QR code</button>
            {share && <><a className="block break-all text-sm underline" href={share}>{share}</a>{qr && <img src={qr} alt="Subscription escrow payment link QR code" width={240} height={240} />}</>}</div>
          </details>
          <details className="group w-full"><summary className={tabs}>Receipts for accounting{chevron}</summary>
            <div className="space-y-2 px-3 pb-3 pt-1 text-sm">
              <p className="text-slate-500">Company details are used for this download only. Chain records prove deposits, collections and refunds; these exports are not tax invoices.</p>
              <div className="grid gap-2 sm:grid-cols-3"><label>Company<input className={input} value={company} onChange={e => setCompany(e.target.value)} /></label><label>Company address<input className={input} value={address} onChange={e => setAddress(e.target.value)} /></label><label>Tax ID<input className={input} value={taxId} onChange={e => setTaxId(e.target.value)} /></label></div>
              {receipts.error && <p role="alert">Receipt history unavailable: {receipts.error.message}</p>}
              {receipts.data && <><button className={button} disabled={!company.trim()} onClick={() => { const url = URL.createObjectURL(new Blob([receiptsCsv(receipts.data!, company, address, taxId, sub.symbol)], { type: 'text/csv;charset=utf-8' })); const a = document.createElement('a'); a.href = url; a.download = 'suifleet-subscription-receipts.csv'; a.click(); URL.revokeObjectURL(url); }}>Download CSV receipts</button>
                {receipts.data.length ? <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-slate-500"><tr><th className="py-1 pr-3">Date</th><th className="pr-3">Type</th><th className="pr-3">Amount</th><th>Transaction</th></tr></thead><tbody className="divide-y">
                  {receipts.data.map((r, i) => <tr key={`${r.digest}:${i}`}><td className="py-1 pr-3 whitespace-nowrap">{receiptDate(r.timestampMs)}</td><td className="pr-3">{r.kind}{r.throughMs !== null && <> · Collected through {receiptDate(r.throughMs)}</>}</td><td className="pr-3 whitespace-nowrap">{formatToken(BigInt(r.amount), coin(sub.symbol).decimals)} {sub.symbol}</td><td className="break-all font-mono">{r.digest} <span className="text-slate-400">· checkpoint {r.checkpoint} · event {r.eventIndex}</span></td></tr>)}
                </tbody></table></div> : <p className="text-slate-500">No receipts yet.</p>}</>}
            </div>
          </details>
        </div>
      </>}
    </section>}

    <section className={card} aria-labelledby="plans-heading">
      <div>
        <h2 id="plans-heading" className="text-lg font-semibold">{sub && !sub.account.cancelled ? 'Renew or change plan' : 'Choose a plan'}</h2>
        <p className="text-sm text-slate-500">{sub && !sub.account.cancelled ? `You can renew after your current term ends on ${dateOnly(sub.account.term_end_ms)}.` : 'Device slots for your whole organization. Prices are fixed for the term you buy.'}</p>
      </div>
      {config.isPending && <p className="text-slate-500">Loading plans…</p>}
      {config.error && <p role="alert">Subscription payments are not configured for this deployment. Contact your fleet administrator.</p>}
      {config.data && !plans.length && <p>No reviewed subscription offers are configured for this deployment. No payment can be requested.</p>}
      <PlansWrap collapsed={renewLater} summary={tabs} chevron={chevron}>
      {plans.length > 0 && <div role="radiogroup" aria-label="Plan" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {plans.map(({ q, choice }) => { const d = choice.data, chosen = selected === q.offerId;
          return <button key={q.offerId} role="radio" aria-checked={chosen} disabled={!d} onClick={() => { setSelected(q.offerId); setMessage(''); }}
            className={`relative !min-h-0 !rounded-2xl !border !p-5 text-left transition ${chosen ? '!border-[#4DA2FF] !bg-blue-50/60 ring-2 ring-[#4DA2FF]/30' : '!border-slate-200 hover:!border-slate-400 hover:!bg-white'} disabled:opacity-50`}>
            <span aria-hidden className={`absolute right-4 top-4 grid h-5 w-5 place-items-center rounded-full border-2 ${chosen ? 'border-[#4DA2FF] bg-[#4DA2FF]' : 'border-slate-300'}`}>{chosen && <span className="h-2 w-2 rounded-full bg-white" />}</span>
            {d ? <><span className="flex gap-2 text-xs font-medium"><span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-700">{d.annual ? 'Annual' : 'Monthly'}</span><span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-700">Pay with {q.symbol}</span></span>
              <span className="mt-4 block text-3xl font-semibold tracking-tight text-slate-900">${d.usd.toFixed(2)}<span className="text-base font-normal text-slate-500"> / {d.annual ? 'year' : 'month'}</span></span>
              <span className="mt-1 block text-sm text-slate-500">{formatToken(d.total, d.descriptor.decimals)} {q.symbol} for the term</span>
              <span className="mt-4 block border-t pt-3 font-medium text-slate-800">Up to {d.offer.device_limit} devices</span></>
              : <span className="text-sm text-slate-500">{q.symbol} plan {choice.error ? 'unavailable' : 'checking…'}</span>}
          </button>; })}
      </div>}
      {offer.error && <p role="alert">{offer.error.message}</p>}
      {offer.data && quote && <div className="flex flex-col gap-3 rounded-xl bg-slate-900 p-4 text-white sm:flex-row sm:items-center sm:justify-between">
        <div className="text-sm"><p className="font-semibold">{offer.data.offer.device_limit} devices · {offer.data.annual ? 'annual' : 'monthly'}</p><p className="text-slate-300">Total {formatToken(offer.data.total, offer.data.descriptor.decimals)} {quote.symbol} (${offer.data.usd.toFixed(2)}) · offer valid until {dateOnly(offer.data.offer.expires_at_ms)}</p>
          {wallet && customerId && !selection.fleet?.administrator && <p className="text-amber-300">Only a customer administrator can buy a plan.</p>}</div>
        <div className="space-y-2 sm:max-w-sm sm:text-right"><button className={`${primary} !border-0 !bg-[#4DA2FF] hover:!bg-[#2e8ed4]`} disabled={!wallet || !customerId || !selection.fleet?.administrator || (busy || mutation.isPending)} onClick={() => void buy(quote)}>Pay {formatToken(offer.data.total, offer.data.descriptor.decimals)} {quote.symbol}</button>{actionStatus('prepay')}</div>
      </div>}
      {offer.data && quote?.symbol === 'USDC' && wallet && customerId && <SubscriptionCard key={context} client={client} quote={quote} wallet={wallet.address} customer={customerId} disabled={!selection.fleet?.administrator || busy || mutation.isPending} prepay={submitted => buy(quote, submitted)} />}
      {offer.data && quote && <p className="text-xs text-slate-500">Your wallet asks for two approvals on a first purchase: the payment, then starting your first period.</p>}
      </PlansWrap>
      {!wallet && <p>Connect a wallet to prepay a plan or review your subscription.</p>}
      <div className="space-y-2 border-t pt-4 text-xs text-slate-500">
        <p>Plans cover device slots only; your device operating budget is separate. Device reporting, uploads and storage are paid from the <a href="/budget/" className="underline">device operating budget</a>; extra reporting, data and encrypted delivery are priced in Fleet settings. This deployment’s checkout and settings tariffs do not establish the all-in commercial offer. Need more capacity or a written quote? <a href="https://suifleet.io/contact/" className="underline">Contact</a> us.</p>
        <details><summary className="cursor-pointer font-medium text-slate-600">Price list and terms</summary>
          <div className="mt-2 space-y-2">
            {price.error && <p role="alert">{price.error}</p>}
            {price.table && <><div className="overflow-x-auto"><table className="w-full text-left"><thead className="text-slate-500"><tr><th className="py-1 pr-3">Devices</th><th className="pr-3">Per device / month</th><th className="pr-3">Full tier, monthly</th><th>Full tier, annual</th></tr></thead><tbody className="divide-y text-slate-700">
              {price.table.base.tiers.map(t => <tr key={t.upToDevices}><td className="py-1 pr-3">Up to {t.upToDevices.toLocaleString('en-US')}</td><td className="pr-3">${t.usdPerDeviceMonth}</td><td className="pr-3">${planUsd(t.upToDevices, false).toFixed(2)}</td><td>${planUsd(t.upToDevices, true).toFixed(2)}</td></tr>)}
            </tbody></table></div>
            <p>Graduated tiers. Monthly minimum ${price.table.base.monthlyMinimumUsd}; annual prepay discount {price.table.base.annualPrepayDiscountPercent}%. Monthly terms are 30 days; annual terms are 365 days. Custom capacity needs a reviewed quote. Source: {price.table.source.replace(/docs\/[A-Za-z0-9_./-]+/g, 'SuiFleet')}.</p></>}
            {quote && <p>Conversion for the selected plan: {quote.source}, {new Date(quote.observedAt).toISOString()}, ${quote.usdPerCoin} USD per {quote.symbol}. The coin price is fixed for the purchased term.</p>}
            {config.data && <p className="break-all">Payments go to the treasury pinned in this deployment’s manifest: <code>{config.data.treasury}</code>{label.data && <> ({label.data})</>}. No SuiNS lookup chooses the recipient.</p>}
            <p>Opening and renewal require a customer administrator, and configured organization protections still apply; this checkout cannot bypass protected-action requirements. Renewal requires a 30-day-old published offer and a settled ended term.</p>
          </div>
        </details>
      </div>
    </section>

    {linkError && <p role="alert">Payment link refused: {linkError}</p>}
    {linkPayment && <section className={card}><h2 className="text-lg font-semibold">Pay a subscription from this wallet</h2>
      <p>Deposit <b>{formatToken(BigInt(linkPayment.amount), coin(linkPayment.symbol).decimals)} {linkPayment.symbol}</b> into an existing subscription. This funds the account; it does not choose or renew a plan, and the customer administrator can refund unearned funds. Check the customer and amount before signing.</p>
      <p className="break-all text-xs text-slate-500">Customer: {linkPayment.customer}<br />Subscription account: {linkPayment.account}{target.data && <><br />Verified treasury: {target.data.treasury}</>}</p>
      {target.error && <p role="alert">{target.error.message}</p>}
      {target.data?.cancelled && <p className="text-amber-800">Account cancelled: deposits remain refundable and do not reactivate it.</p>}
      <button className={primary} disabled={!wallet || !target.data || (busy || mutation.isPending)} onClick={() => void run('deposit', () => new Transaction(), linkPayment)}>Approve subscription deposit</button>{actionStatus('deposit')}
      <details className="text-sm"><summary className="cursor-pointer">Paid from another wallet?</summary><div className="mt-2 space-y-2"><label className="block">Transaction digest<input className={input} value={digest} onChange={e => setDigest(e.target.value)} /></label><button className={button} disabled={!digest || !target.data} onClick={() => void confirmLink()}>Verify deposit from another wallet</button></div></details>
    </section>}
    {!activeAction && message && <p role="status">{message}</p>}
  </>;
}

function PlansWrap({ collapsed, summary, chevron, children }: { collapsed: boolean; summary: string; chevron: React.ReactNode; children: React.ReactNode }) {
  return collapsed ? <details className="group -mx-3"><summary className={summary}>See available plans{chevron}</summary><div className="space-y-5 px-3 pt-3">{children}</div></details> : <>{children}</>;
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="bg-white px-4 py-3"><dt className="text-xs font-medium text-slate-500">{label}</dt><dd className="mt-0.5 font-semibold text-slate-900">{value}</dd></div>;
}
