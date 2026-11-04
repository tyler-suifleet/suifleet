"use client";
import { useEffect, useRef, useState } from 'react';
import { useCurrentClient } from '@mysten/dapp-kit-react';
import { useQueries, useQuery } from '@tanstack/react-query';
import QRCode from 'qrcode';
import { useCurrentAccount } from '@/hooks/useCurrentAccount';
import { useFleetSelection } from '@/hooks/useFleetSelection';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { PageHeader } from '@/components/ui';
import { formatToken, parseToken } from '@/lib/network-costs';
import { loadPriceTable } from '@/lib/fleet-pricing';
import { requireManifest, treasuryPayee } from '@/lib/deployment-manifest';
import { Transaction } from '@mysten/sui/transactions';
import { quotes, readOffer, customerAccount, planUsd, preparePrepay, treasuryLabel, coin, paymentUri, parsePayment, readAccount, verifyDeposit, verifyPlan, receiptsCsv, receiptDate, history, warnings, collectTx, type Payment, type Quote } from '@/lib/subscription';

const button = 'rounded-lg border border-blue-300 px-4 py-2 text-sm font-medium disabled:opacity-50';
const input = 'block w-full rounded border border-slate-300 p-2';
const time = (ms: string) => new Date(Number(ms)).toISOString();
export default function BillingPage() {
  return <div className="max-w-5xl space-y-6"><PageHeader title="Plans and billing" subtitle="Choose device capacity, prepay a fixed term and review subscription receipts." /><Billing /></div>;
}
function Billing() {
  const client = useCurrentClient(), wallet = useCurrentAccount(), selection = useFleetSelection(), mutation = useFleetTransaction();
  const [busy, setBusy] = useState(false);
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
  async function run(action: () => Promise<Transaction> | Transaction, payment?: Payment, plan?: Quote) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    const captured = context;
    setMessage('Verifying payment and requesting wallet approval…');
    try {
      const tx = await action();
      if (latest.current !== captured) throw Error("Account or fleet changed; retry");
      const result = await mutation.mutateAsync({ transaction: tx, subscriptionDeposit: payment, onSubmitted: d => { if (latest.current !== captured) return; setDigest(d); setMessage('Submitted; checking on-chain effects. Do not pay again while confirmation is uncertain.'); } });
      if (plan && customerId) await verifyPlan(client, result.digest, customerId, plan);
      if (payment) await verifyDeposit(client, result.digest, payment);
      if (latest.current !== captured) return;
      setDigest(result.digest); setMessage('Confirmed successful on-chain execution.');
      if (customerId) await current.refetch(); if (linkPayment) await target.refetch(); if (current.data) await receipts.refetch();
    } catch (e) { if (latest.current === captured) setMessage(`${e instanceof Error ? e.message : String(e)}. A digest alone is not payment confirmation. Check a submitted digest before retrying.`); } finally { inFlight.current = false; setBusy(false); }
  }
  async function sharePayment() {
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
    const captured = context;
    setMessage('Checking the submitted transaction…');
    try { await verifyDeposit(client, digest, linkPayment!); if (latest.current === captured) { setMessage('Subscription deposit confirmed on chain.'); await target.refetch(); } }
    catch (e) { if (latest.current === captured) setMessage(String(e)); }
  }
  return <>
    <section className="rounded-xl border bg-white p-5 space-y-3"><h2 className="text-xl font-semibold">Capacity plans</h2><p>The platform subscription covers fleet management at the included reporting and retention settings. Device gas, uploads and storage are paid separately from the <a href="/budget/" className="underline">network budget</a>. Extra reporting, data and encrypted delivery are priced in Fleet settings. Activation is separately quoted; this checkout buys only the base subscription.</p>
      {price.error && <p role="alert">{price.error}</p>}
      {price.table && <><p className="text-sm">Pricing source: {price.table.source.replace(/docs\/[A-Za-z0-9_./-]+/g, 'SuiFleet')}</p><ul>{price.table.base.tiers.map(t => <li key={t.upToDevices}>Up to {t.upToDevices.toLocaleString('en-US')} devices: ${t.usdPerDeviceMonth} per device/month in this graduated tier; base term at full capacity ${planUsd(t.upToDevices, false).toFixed(2)} monthly or ${planUsd(t.upToDevices, true).toFixed(2)} annual.</li>)}</ul><p>Monthly minimum ${price.table.base.monthlyMinimumUsd}; annual prepay discount {price.table.base.annualPrepayDiscountPercent}%. Custom capacity needs a reviewed quote. Monthly terms are 30 days; annual terms are 365 days.</p></>}
      {config.isPending && <p>Loading subscription configuration…</p>}{config.error && <p role="alert">Subscription payments are not configured for this deployment. Contact your fleet administrator.</p>}
      {config.data && <><p className="break-all">Treasury payee: <code>{config.data.treasury}</code>{label.data && <span> ({label.data})</span>}</p><p className="text-sm">Payments use this manifest-pinned address. No SuiNS lookup chooses the recipient.</p>
      {!config.data.quotes.length ? <p>No reviewed subscription offers are configured for this deployment. No payment can be requested.</p> : <label>Plan and payment coin<select className={input} value={selected} onChange={e => { setSelected(e.target.value); setMessage(''); }}><option value="">Choose a reviewed offer</option>{config.data.quotes.map((q, i) => <option key={q.offerId} value={q.offerId} disabled={!choices[i].data}>{choices[i].data ? `${choices[i].data!.offer.device_limit} devices · ${choices[i].data!.annual ? 'Annual' : 'Monthly'} · ${formatToken(choices[i].data!.total, choices[i].data!.descriptor.decimals)} ${q.symbol}` : `${q.symbol} plan ${choices[i].error ? 'unavailable' : 'verifying…'}`}</option>)}</select></label>}</>}
      {offer.error && <p role="alert">{offer.error.message}</p>}
      {offer.data && quote && <><p>Tier {offer.data.offer.tier} · {offer.data.offer.device_limit} devices · allowance {offer.data.offer.allowance} · {offer.data.annual ? 'Annual' : 'Monthly'}: ${offer.data.usd.toFixed(2)} USD / {formatToken(offer.data.total, offer.data.descriptor.decimals)} {quote.symbol}.</p><p>Conversion: {quote.source}, {new Date(quote.observedAt).toISOString()}, ${quote.usdPerCoin} USD per {quote.symbol}. Offer expires {time(offer.data.offer.expires_at_ms)}. Coin price is fixed for the purchased term.</p><button className={button} disabled={!wallet || !customerId || !selection.fleet?.administrator || (busy || mutation.isPending)} onClick={() => void run(() => preparePrepay(client, quote, customerId!, wallet!.address), undefined, quote)}>Prepay from connected wallet in one transaction</button><p>Opening and renewal require a customer administrator. Configured organization protections still apply. This checkout cannot bypass protected-action requirements; use your organization’s approved signing process when additional factors are required. Renewal requires a 30-day-old published offer and a settled ended term.</p></>}
      {!wallet && <p>Connect a wallet to prepay or review your subscription.</p>}
    </section>
    {linkError && <p role="alert">Payment link refused: {linkError}</p>}
    {linkPayment && <section className="rounded-xl border bg-white p-5 space-y-3"><h2 className="text-xl font-semibold">Pay a subscription from this wallet</h2><p className="break-all">Customer: {linkPayment.customer}<br />Escrow account: {linkPayment.account}</p><p>Deposit {formatToken(BigInt(linkPayment.amount), coin(linkPayment.symbol).decimals)} {linkPayment.symbol}. This funds an existing subscription account; it does not choose or renew a plan. The customer administrator can refund unearned funds. Verify the customer and amount before signing.</p>{target.error && <p role="alert">{target.error.message}</p>}{target.data && <p className="break-all">Verified treasury: {target.data.treasury}{target.data.cancelled ? ' · Account cancelled: deposits remain refundable and do not reactivate it.' : ''}</p>}<button className={button} disabled={!wallet || !target.data || (busy || mutation.isPending)} onClick={() => void run(() => new Transaction(), linkPayment)}>Approve escrow deposit</button><label>Transaction digest<input className={input} value={digest} onChange={e => setDigest(e.target.value)} /></label><button className={button} disabled={!digest || !target.data} onClick={() => void confirmLink()}>Verify deposit from another wallet</button></section>}
    {current.error && <p role="alert">Subscription status unavailable: {current.error.message}</p>}
    {current.data === null && <p>No subscription account exists for this customer. An administrator must open and fund the first term before another wallet can deposit.</p>}
    {current.data && <section className="rounded-xl border bg-white p-5 space-y-3"><h2 className="text-xl font-semibold">Your subscription</h2><p>Tier {current.data.account.tier} · {current.data.account.device_limit} devices · {current.data.symbol}</p><p>Prepaid term ends: {time(current.data.account.term_end_ms)}. Collected paid-through boundary: {time(current.data.account.next_due_ms)}. Balance: {formatToken(BigInt(current.data.account.balance.value), coin(current.data.symbol).decimals)} {current.data.symbol}. Remaining collections: {current.data.account.remaining}. Next collection: {current.data.account.remaining === '0' || current.data.account.cancelled ? 'None' : time(current.data.account.next_due_ms)}.</p>
      {warnings(current.data.account).map(w => <p role="status" key={w}>{w}</p>)}<button className={button} disabled={(busy || mutation.isPending) || current.data.account.cancelled || current.data.account.remaining === '0' || Date.now() < Number(current.data.account.next_due_ms)} onClick={() => void run(() => collectTx(current.data!.account, current.data!.symbol))}>Collect due period</button>
      <h3 className="font-semibold">Request funds from another wallet</h3><label>Amount in {current.data.symbol}<input className={input} inputMode="decimal" value={amount} onChange={e => { setAmount(e.target.value); setShare(''); setQr(''); }} /></label><button className={button} onClick={() => void sharePayment()}>Create payment link and QR code</button>{share && <><a className="block break-all underline" href={share}>{share}</a>{qr && <img src={qr} alt="Subscription escrow payment link QR code" width={320} height={320} />}</>}
      <h3 className="font-semibold">Accounting receipts</h3><p>Company details are used for this download only. Chain records prove deposits, collections and refunds; these exports are not tax invoices.</p><label>Company<input className={input} value={company} onChange={e => setCompany(e.target.value)} /></label><label>Company address<input className={input} value={address} onChange={e => setAddress(e.target.value)} /></label><label>Tax ID<input className={input} value={taxId} onChange={e => setTaxId(e.target.value)} /></label>
      {receipts.error && <p role="alert">Receipt history unavailable: {receipts.error.message}</p>}{receipts.data && <><button className={button} disabled={!company.trim()} onClick={() => { const url = URL.createObjectURL(new Blob([receiptsCsv(receipts.data!, company, address, taxId, current.data!.symbol)], { type: 'text/csv;charset=utf-8' })); const a = document.createElement('a'); a.href = url; a.download = 'suifleet-subscription-receipts.csv'; a.click(); URL.revokeObjectURL(url); }}>Download CSV receipts</button><ul>{receipts.data.map((r, i) => <li key={`${r.digest}:${i}`} className="break-all">{receiptDate(r.timestampMs)} · checkpoint {r.checkpoint} · event {r.eventIndex} · {r.kind}: {formatToken(BigInt(r.amount), coin(current.data!.symbol).decimals)} {current.data!.symbol} · {r.digest}{r.throughMs !== null && <> · Collected through {receiptDate(r.throughMs)}</>}</li>)}</ul></>}
    </section>}
    {message && <p role="status">{message}</p>}{digest && <p className="break-all">Submitted digest: {digest}</p>}
  </>;
}
