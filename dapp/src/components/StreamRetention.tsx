"use client";
import { useEffect, useRef, useState } from 'react';
import { useCurrentAccount, useCurrentClient } from '@mysten/dapp-kit-react';
import { useFleetDataConfig } from '@/hooks/useFleetDataConfig';
import { useFleetSelection } from '@/hooks/useFleetSelection';
import { fetchCustomerFleets } from '@/lib/customer-fleets';
import { selectedDataAuthority } from '@/lib/data-authority';
import { fetchBudgets, fetchManagedBudget, storageConfig } from '@/lib/fleet-budget';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { ARCHIVE_STREAMS, readStreamPolicies, setStreamRetentionTx, extendStreamArchiveTx } from '@/lib/stream-retention';

export function StreamRetentionPanel() {
  const DATA_CONFIG = useFleetDataConfig(), selection = useFleetSelection();
  const client = useCurrentClient(), account = useCurrentAccount();
  const { mutateAsync: execute, isPending } = useFleetTransaction();
  const [values, setValues] = useState<(number | undefined)[]>([]), [kind, setKind] = useState(0), [epochs, setEpochs] = useState(3);
  const [cap, setCap] = useState<string>(), [message, setMessage] = useState(''), [loaded, setLoaded] = useState(false);
  const [blob, setBlob] = useState(''), [extension, setExtension] = useState(1);
  const storage = storageConfig().config;
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current; setCap(undefined); setValues([]); setLoaded(false); setMessage('');
    if (!DATA_CONFIG?.streamRetentionOrigin || !account) return;
    const config = DATA_CONFIG;
    void Promise.all([readStreamPolicies(client, config), (async () => {
      if (!selection.supported) return fetchBudgets(client, account.address);
      const authority = selectedDataAuthority(await fetchCustomerFleets(client, account.address), selection.fleet, config.budgetId);
      return authority.kind === 'customer' ? [await fetchManagedBudget(client, config.budgetId)] : [];
    })()]).then(([policies, budgets]) => {
      if (current !== generation.current) return;
      setValues(policies); setLoaded(true); setCap(budgets.find(b => normalizeSuiAddress(b.id) === config.budgetId)?.capId);
    }).catch(() => { if (current === generation.current) setMessage('Retention policy unavailable.'); });
    return () => { generation.current++; };
  }, [client, account?.address, DATA_CONFIG, selection.identity]); // eslint-disable-line react-hooks/exhaustive-deps
  async function save() {
    if (!DATA_CONFIG || !cap) return;
    const current = generation.current;
    try {
      await execute({ transaction: setStreamRetentionTx(DATA_CONFIG, cap, kind, epochs) });
      const policies = await readStreamPolicies(client, DATA_CONFIG);
      if (current === generation.current) { setValues(policies); setMessage('Retention confirmed for new writes. Existing expiries are unchanged.'); }
    } catch { if (current === generation.current) setMessage('Retention change refused or unavailable. Re-read chain state before retrying.'); }
  }
  async function extend() {
    if (!DATA_CONFIG || !cap || !storage) return;
    const current = generation.current;
    try {
      await execute({transaction:extendStreamArchiveTx(DATA_CONFIG,storage,cap,blob,extension)});
      if (current === generation.current) setMessage('Archive extension confirmed. Its live storage record controls expiry.');
    } catch { if (current === generation.current) setMessage('Archive extension refused or unavailable. Expired bytes and pending renewal policies cannot be extended here.'); }
  }
  return <section className="border rounded p-4 space-y-3"><h2 className="font-semibold">Per-stream retention</h2>
    {!DATA_CONFIG?.streamRetentionOrigin ? <p>This deployment needs the stream retention upgrade and verified type origin.</p> : <>
      <ul>{ARCHIVE_STREAMS.map((name, i) => <li key={name}>{name}: {loaded ? values[i] === undefined ? 'legacy pool policy; not configured' : `${values[i]} Walrus epochs` : 'unavailable'}</li>)}</ul>
      <label>Stream <select value={kind} onChange={e => { const next = Number(e.target.value); setKind(next); setEpochs(values[next] ?? 3); }}>{ARCHIVE_STREAMS.map((name, i) => <option key={name} value={i}>{name}</option>)}</select></label>
      <label> Retention in Walrus epochs <input type="number" min={1} max={53} step={1} value={epochs} onChange={e => setEpochs(Number(e.target.value))} /></label>
      <button className="border rounded p-2" disabled={!loaded || !cap || isPending} onClick={() => void save()}>Save stream retention</button>
      <div><label>Existing archive blob object ID <input value={blob} onChange={e => setBlob(e.target.value)} /></label>
        <label> Additional epochs <input type="number" min={1} max={53} step={1} value={extension} onChange={e => setExtension(Number(e.target.value))} /></label>
        <button className="border rounded p-2" disabled={!cap || !storage || isPending} onClick={() => void extend()}>Extend archive before expiry</button></div>
      <p>New device writes buy the full period, within their WAL and upload quotas. Dates depend on the network’s Walrus epoch schedule. Choose 1–53 epochs; keep an existing archive longer with a wallet-approved extension before expiry, within the live Walrus horizon. Expired bytes cannot be restored by extending a reference. Recover expired deposits in Network budget after reviewing the reclaim preview. The cost estimate uses the fleet-wide pool setting; per-stream purchases also need enough WAL in the device’s existing allowance.</p>
    </>}{message && <p role="status">{message}</p>}
  </section>;
}
