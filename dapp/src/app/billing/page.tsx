"use client";
import { useEffect, useRef, useState } from 'react';
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
const card = 'rounded-xl border bg-white p-5 space-y-4';
const input = 'mt-1 block w-full rounded border border-slate-300 p-2';
export default function BillingPage() {
  return <div className="max-w-5xl space-y-6"><PageHeader title="Plans and billing" subtitle="Buy device slots for your organization and manage your subscription." /><Billing /></div>;
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
  async function run(actionName: string, action: () => Promise<Transaction> | Transaction, payment?: Payment, plan?: Quote, submitted?: (digest: string) => void) {
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
      setDigest(result.digest); setMessage('Confirmed. If a prepaid payment is due below, apply it to activate your device capacity.');
      if (customerId) { await current.refetch(); selection.refresh(); } if (linkPayment) await target.refetch(); if (current.data) await receipts.refetch();
      return true;
    } catch (e) { if (latest.current === captured) setMessage(`${e instanceof Error ? e.message : String(e)}. A digest alone is not payment confirmation. Check a submitted digest before retrying.`); return false; } finally { inFlight.current = false; setBusy(false); }
  }
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
  const actionStatus = (name: string) => activeAction === name && message ? <p role="status" className="break-words text-sm" aria-live="polite">{message}{digest && <span className="block break-all">Submitted digest: {digest}</span>}</p> : null;
  const fleet = selection.fleet as (typeof selection.fleet & { activeDevices?: bigint; deviceLimit?: bigint }) | undefined;
  const plans = (config.data?.quotes ?? []).map((q, i) => ({ q, choice: choices[i] }));
  const sub = current.data;
  const due = !!sub && !sub.account.cancelled && sub.account.remaining !== '0' && Date.now() >= Number(sub.account.next_due_ms);
  const dateOnly = (ms: string) => new Date(Number(ms)).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  return <>

    {wallet && customerId && <section className={card} aria-labelledby="plan-status">
      <h2 id="plan-status" className="text-lg font-semibold">Your plan</h2>
      {current.isPending && <p className="text-slate-500">Loading your subscription…</p>}
      {current.error && <p role="alert">Subscription status unavailable: {current.error.message}</p>}
      {sub === null && <p>No plan yet{fleet?.deviceLimit !== undefined ? ` · ${fleet.activeDevices} of ${fleet.deviceLimit} device slots in use` : ''}. Choose a plan below to add device slots. An administrator opens and funds the first term.</p>}
      {sub && <>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Device slots" value={sub.account.device_limit} />
          <Stat label="Paid through" value={dateOnly(sub.account.next_due_ms)} />
          <Stat label="Term ends" value={dateOnly(sub.account.term_end_ms)} />
          <Stat label="Balance" value={`${formatToken(BigInt(sub.account.balance.value), coin(sub.symbol).decimals)} ${sub.symbol}`} />
        </dl>
        {warnings(sub.account).map(w => <p role="status" key={w} className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">{w}</p>)}
        {due && <div className="space-y-1">
          <button className={primary} disabled={busy || mutation.isPending} onClick={() => void run('collect', () => collectTx(sub.account, sub.symbol))}>Apply due prepaid payment</button>{actionStatus('collect')}
          <p className="text-xs text-slate-500">Uses money already in your subscription balance to activate or extend device slots. Your wallet is not charged again; approving costs a small network fee.</p>
        </div>}
        <details className="border-t pt-3"><summary className="cursor-pointer font-medium">Ask another wallet to pay</summary>
          <div className="mt-2 space-y-2"><label className="block text-sm">Amount in {sub.symbol}<input className={input} inputMode="decimal" value={amount} onChange={e => { setAmount(e.target.value); setShare(''); setQr(''); }} /></label>
          <button className={button} onClick={() => void sharePayment()}>Create payment link and QR code</button>
          {share && <><a className="block break-all text-sm underline" href={share}>{share}</a>{qr && <img src={qr} alt="Subscription escrow payment link QR code" width={240} height={240} />}</>}</div>
        </details>
        <details className="border-t pt-3"><summary className="cursor-pointer font-medium">Receipts for accounting</summary>
          <div className="mt-2 space-y-2 text-sm">
            <p className="text-slate-500">Company details are used for this download only. Chain records prove deposits, collections and refunds; these exports are not tax invoices.</p>
            <div className="grid gap-2 sm:grid-cols-3"><label>Company<input className={input} value={company} onChange={e => setCompany(e.target.value)} /></label><label>Company address<input className={input} value={address} onChange={e => setAddress(e.target.value)} /></label><label>Tax ID<input className={input} value={taxId} onChange={e => setTaxId(e.target.value)} /></label></div>
            {receipts.error && <p role="alert">Receipt history unavailable: {receipts.error.message}</p>}
            {receipts.data && <><button className={button} disabled={!company.trim()} onClick={() => { const url = URL.createObjectURL(new Blob([receiptsCsv(receipts.data!, company, address, taxId, sub.symbol)], { type: 'text/csv;charset=utf-8' })); const a = document.createElement('a'); a.href = url; a.download = 'suifleet-subscription-receipts.csv'; a.click(); URL.revokeObjectURL(url); }}>Download CSV receipts</button>
              {receipts.data.length ? <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-slate-500"><tr><th className="py-1 pr-3">Date</th><th className="pr-3">Type</th><th className="pr-3">Amount</th><th>Transaction</th></tr></thead><tbody className="divide-y">
                {receipts.data.map((r, i) => <tr key={`${r.digest}:${i}`}><td className="py-1 pr-3 whitespace-nowrap">{receiptDate(r.timestampMs)}</td><td className="pr-3">{r.kind}{r.throughMs !== null && <> · Collected through {receiptDate(r.throughMs)}</>}</td><td className="pr-3 whitespace-nowrap">{formatToken(BigInt(r.amount), coin(sub.symbol).decimals)} {sub.symbol}</td><td className="break-all font-mono">{r.digest} <span className="text-slate-400">· checkpoint {r.checkpoint} · event {r.eventIndex}</span></td></tr>)}
              </tbody></table></div> : <p className="text-slate-500">No receipts yet.</p>}</>}
          </div>
        </details>
      </>}
    </section>}

    <section className={card}>
      <h2 className="text-lg font-semibold">Capacity plans</h2>
      {config.isPending && <p className="text-slate-500">Loading plans…</p>}
      {config.error && <p role="alert">Subscription payments are not configured for this deployment. Contact your fleet administrator.</p>}
      {config.data && !plans.length && <p>No reviewed subscription offers are configured for this deployment. No payment can be requested.</p>}
      {plans.length > 0 && <div role="radiogroup" aria-label="Plan" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {plans.map(({ q, choice }) => { const d = choice.data, chosen = selected === q.offerId;
          return <button key={q.offerId} role="radio" aria-checked={chosen} disabled={!d} onClick={() => { setSelected(q.offerId); setMessage(''); }}
            className={`rounded-xl border p-4 text-left transition ${chosen ? 'border-[#4DA2FF] ring-2 ring-[#4DA2FF]/40 bg-blue-50/40' : 'border-slate-200 hover:border-slate-400'} disabled:opacity-50`}>
            {d ? <><span className="block text-sm text-slate-500">{d.annual ? 'Annual' : 'Monthly'} · pay in {q.symbol}</span>
              <span className="mt-1 block text-2xl font-semibold">{d.offer.device_limit} devices</span>
              <span className="mt-1 block">${d.usd.toFixed(2)} <span className="text-slate-500">· {formatToken(d.total, d.descriptor.decimals)} {q.symbol}</span></span></>
              : <span className="text-sm text-slate-500">{q.symbol} plan {choice.error ? 'unavailable' : 'checking…'}</span>}
          </button>; })}
      </div>}
      {offer.error && <p role="alert">{offer.error.message}</p>}
      {offer.data && quote && <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-4">
        <p><b>{offer.data.offer.device_limit} devices, {offer.data.annual ? 'annual' : 'monthly'}</b> · ${offer.data.usd.toFixed(2)} ({formatToken(offer.data.total, offer.data.descriptor.decimals)} {quote.symbol}) · offer valid until {dateOnly(offer.data.offer.expires_at_ms)}</p>
        {wallet && customerId && !selection.fleet?.administrator && <p className="text-sm text-amber-800">Only a customer administrator can buy a plan.</p>}
        <button className={primary} disabled={!wallet || !customerId || !selection.fleet?.administrator || (busy || mutation.isPending)} onClick={() => void run('prepay', () => preparePrepay(client, quote, customerId!, wallet!.address), undefined, quote)}>Pay {formatToken(offer.data.total, offer.data.descriptor.decimals)} {quote.symbol} from your wallet</button>{actionStatus('prepay')}
        {quote.symbol === 'USDC' && wallet && customerId && <SubscriptionCard key={context} client={client} quote={quote} wallet={wallet.address} customer={customerId} disabled={!selection.fleet?.administrator || busy || mutation.isPending} prepay={submitted => run('prepay', () => preparePrepay(client, quote, customerId, wallet.address), undefined, quote, submitted)} />}
      </div>}
      {!wallet && <p>Connect a wallet to prepay or review your subscription.</p>}
      <p className="text-xs text-slate-500">This checkout buys only the base subscription; your device operating budget is separate. Device reporting, uploads and storage are paid from the <a href="/budget/" className="underline">device operating budget</a> and storage budget; extra reporting, data and encrypted delivery are priced in Fleet settings, and activation is quoted separately. This deployment’s checkout and settings tariffs do not establish the all-in commercial offer. For a written quote or private pilot, <a href="https://suifleet.io/contact/" className="underline">Contact</a> us.</p>
      <details className="text-sm"><summary className="cursor-pointer font-medium">Price list and terms</summary>
        <div className="mt-2 space-y-2">
          {price.error && <p role="alert">{price.error}</p>}
          {price.table && <><div className="overflow-x-auto"><table className="w-full text-left"><thead className="text-xs text-slate-500"><tr><th className="py-1 pr-3">Devices</th><th className="pr-3">Per device / month</th><th className="pr-3">Full tier, monthly</th><th>Full tier, annual</th></tr></thead><tbody className="divide-y">
            {price.table.base.tiers.map(t => <tr key={t.upToDevices}><td className="py-1 pr-3">Up to {t.upToDevices.toLocaleString('en-US')}</td><td className="pr-3">${t.usdPerDeviceMonth}</td><td className="pr-3">${planUsd(t.upToDevices, false).toFixed(2)}</td><td>${planUsd(t.upToDevices, true).toFixed(2)}</td></tr>)}
          </tbody></table></div>
          <p>Graduated tiers. Monthly minimum ${price.table.base.monthlyMinimumUsd}; annual prepay discount {price.table.base.annualPrepayDiscountPercent}%. Monthly terms are 30 days; annual terms are 365 days. Custom capacity needs a reviewed quote. Source: {price.table.source.replace(/docs\/[A-Za-z0-9_./-]+/g, 'SuiFleet')}.</p></>}
          {quote && <p>Conversion for the selected plan: {quote.source}, {new Date(quote.observedAt).toISOString()}, ${quote.usdPerCoin} USD per {quote.symbol}. The coin price is fixed for the purchased term.</p>}
          {config.data && <p className="break-all">Payments go to the treasury pinned in this deployment’s manifest: <code>{config.data.treasury}</code>{label.data && <> ({label.data})</>}. No SuiNS lookup chooses the recipient.</p>}
          <p>Opening and renewal require a customer administrator, and configured organization protections still apply; this checkout cannot bypass protected-action requirements. Renewal requires a 30-day-old published offer and a settled ended term.</p>
        </div>
      </details>
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

function Stat({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-xs text-slate-500">{label}</dt><dd className="text-lg font-semibold">{value}</dd></div>;
}
