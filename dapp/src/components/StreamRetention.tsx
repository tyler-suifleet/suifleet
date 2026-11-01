"use client";
import { useEffect, useRef, useState } from 'react';
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
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
    } catch { if (current === generation.current) setMessage('Retention change refused or unavailable. Reload the settings before retrying.'); }
  }
  async function extend() {
    if (!DATA_CONFIG || !cap || !storage) return;
    const current = generation.current;
    try {
      await execute({transaction:extendStreamArchiveTx(DATA_CONFIG,storage,cap,blob,extension)});
      if (current === generation.current) setMessage('Archive extension confirmed. The storage network determines the updated expiry.');
    } catch { if (current === generation.current) setMessage('Archive extension refused or unavailable. Expired data and archives awaiting renewal cannot be extended here.'); }
  }
  return <section className="border rounded p-4 space-y-3"><h2 className="font-semibold">Per-stream retention</h2>
    {!DATA_CONFIG?.streamRetentionOrigin ? <p>Retention settings are unavailable until this fleet’s software is upgraded and verified.</p> : <>
      <ul>{ARCHIVE_STREAMS.map((name, i) => <li key={name}>{name}: {loaded ? values[i] === undefined ? 'using the shared storage setting' : 'custom storage period configured (see technical settings)' : 'unavailable'}</li>)}</ul>
      <p>Choose how long to keep each stream’s new data. Changing a setting does not extend existing archives. Exact durations depend on the storage network’s schedule; calendar expiry dates are unavailable here.</p>
      <details><summary>Technical storage settings</summary>
        <p>Storage periods use Walrus epochs, the storage network’s time units. Choose 1–53 epochs. Current settings: {ARCHIVE_STREAMS.map((name, i) => `${name}: ${loaded ? values[i] === undefined ? 'shared setting' : `${values[i]} epochs` : 'unavailable'}`).join('; ')}.</p>
        <label>Stream <select value={kind} onChange={e => { const next = Number(e.target.value); setKind(next); setEpochs(values[next] ?? 3); }}>{ARCHIVE_STREAMS.map((name, i) => <option key={name} value={i}>{name}</option>)}</select></label>
        <label> Storage period (Walrus epochs) <input type="number" min={1} max={53} step={1} value={epochs} onChange={e => setEpochs(Number(e.target.value))} /></label>
        <button className="border rounded p-2" disabled={!loaded || !cap || isPending} onClick={() => void save()}>Save stream retention</button>
        <div><label>Archive storage object ID <input value={blob} onChange={e => setBlob(e.target.value)} /></label>
          <label> Additional storage (Walrus epochs) <input type="number" min={1} max={53} step={1} value={extension} onChange={e => setExtension(Number(e.target.value))} /></label>
          <button className="border rounded p-2" disabled={!cap || !storage || isPending} onClick={() => void extend()}>Extend archive before expiry</button></div>
      </details>
      <p>Your wallet, the app that approves account actions, must approve changes and extensions. New device writes pay for the full storage period using WAL, the asset used to buy storage, within the device’s spending and upload limits. The cost estimate uses the shared storage setting; custom periods also require enough WAL in the device’s existing allowance. Extensions must be approved before expiry and stay within the storage network’s maximum future period. Expired data cannot be restored by extending an archive. Recover eligible expired storage deposits in Network budget after reviewing the reclaim preview.</p>
    </>}{message && <p role="status">{message}</p>}
  </section>;
}
