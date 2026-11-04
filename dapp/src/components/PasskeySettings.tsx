"use client";
import { useState } from 'react';
import { useCurrentClient, useDAppKit } from '@mysten/dapp-kit-react';
import { useQuery } from '@tanstack/react-query';
import { bcs } from '@mysten/sui/bcs';
import { useFleetSelection } from '@/hooks/useFleetSelection';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { MANIFEST, verifyDeployment } from '@/lib/deployment-manifest';
import { createPasskey, preferPasskey, exportPasskeys, importPasskeys, savedPasskeys, passkeyAddress, passkeySigner, passkeySupported, fetchPasskeyEntries, fundPasskeyTx, enrollmentProofTx, enrollPasskeyTx, removePasskeyTx } from '@/lib/passkey-factor';

export function PasskeySettings() {
  const selection = useFleetSelection(), client = useCurrentClient(), wallet = useDAppKit(), mutation = useFleetTransaction();
  const fleet = selection.fleet;
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [revision, setRevision] = useState(0), [proofId, setProofId] = useState(''), [metadata, setMetadata] = useState('');
  const entries = useQuery({ queryKey: ['passkey-entries', fleet?.customerId, revision], enabled: !!fleet && passkeySupported(), queryFn: () => fetchPasskeyEntries(client, fleet!.customerId) });
  async function run(operation: () => Promise<void>) { setBusy(true); setError(''); try { await operation(); setRevision(r => r + 1); } catch(e) { setError(e instanceof Error ? e.message : 'Passkey operation failed'); } finally { setBusy(false); } }
  if (!fleet || !passkeySupported()) return null;
  let local: ReturnType<typeof savedPasskeys> = [];
  try { local = savedPasskeys(); } catch { /* Surface metadata repair alongside on-chain keys. */ }
  const administrator = wallet.stores.$connection.get().account?.address;
  return <section aria-label="Passkey enrollment" className="space-y-3 border p-3">
    <p>Enroll two independently recoverable passkeys before enabling protection. Your passkey sponsors and signs the exact wallet transaction, paying its gas. Initial funding sends 0.01 SUI to that passkey; it is a separate wallet transaction. Keep gas available there.</p>
    <p>Requires HTTPS or localhost, WebAuthn, a P-256 discoverable credential and a wallet supporting transaction signing. A synced key depends on its cloud account. Hardware and platform support varies; final real-wallet and hardware-key acceptance is pending.</p>
    <button disabled={busy || !fleet.administrator} onClick={() => run(async () => { await createPasskey(); })}>Create passkey</button>
    <ul>{local.map(key => <li key={key.publicKey}><code>{passkeyAddress(key)}</code> <button disabled={busy} onClick={() => { preferPasskey(key); setRevision(r => r + 1); }}>Use this key for signing</button>{' '}
      <button disabled={busy || !fleet.administrator} onClick={() => run(async () => { await mutation.mutateAsync({ transaction: fundPasskeyTx(key) }); })}>Fund with 0.01 SUI</button>{' '}
      <button disabled={busy || !fleet.administrator} onClick={() => run(async () => {
        if (!administrator || !MANIFEST) throw Error('Connect an administrator wallet');
        await verifyDeployment(client);
        const transaction = enrollmentProofTx(fleet.customerId, administrator, key, BigInt(Date.now() + 300000));
        const result = await client.signAndExecuteTransaction({ transaction, signer: passkeySigner(key), include: { effects: true, events: true } });
        if (result.FailedTransaction || !result.Transaction.status.success) throw Error('Passkey enrollment proof failed');
        const confirmed = await client.waitForTransaction({ digest: result.Transaction.digest, include: { events: true, effects: true } });
        if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) throw Error('Passkey proof confirmation failed');
        const origin = MANIFEST.type_origins['passkey_factor::ProofCreated'];
        const event = confirmed.Transaction.events?.find(e => e.eventType === `${origin}::passkey_factor::ProofCreated`);
        if (!event) throw Error('Confirmed enrollment proof event missing; reconcile before retrying');
        const proof = bcs.struct('ProofCreated', { proof_id: bcs.Address, customer: bcs.Address, administrator: bcs.Address, key: bcs.Address }).parse(event.bcs);
        if (proof.customer !== fleet.customerId || proof.administrator !== administrator || proof.key !== passkeyAddress(key)) throw Error('Enrollment proof binding mismatch');
        setProofId(proof.proof_id);
      })}>Prove possession</button>
    </li>)}</ul>
    <label>Public credential metadata (backup or restore)<textarea value={metadata} onChange={e => setMetadata(e.target.value)} /></label>
    <button disabled={busy} onClick={() => run(async () => { setMetadata(exportPasskeys()); })}>Show backup metadata</button>
    <button disabled={busy} onClick={() => run(async () => { importPasskeys(metadata); })}>Restore public metadata</button>
    <label>Confirmed enrollment proof ID <input value={proofId} onChange={e => setProofId(e.target.value)} /></label>
    <button disabled={busy || !proofId || !fleet.administrator} onClick={() => run(async () => { await mutation.mutateAsync({ transaction: enrollPasskeyTx(fleet.customerId, proofId) }); setProofId(''); })}>Enroll confirmed passkey</button>
    {entries.error && <p role="alert">{entries.error.message}</p>}
    <ul>{entries.data?.filter((_, i) => i % 2 === 1).map((key, index) => <li key={key}>Administrator {entries.data![index * 2]}: passkey {key}{' '}<button disabled={busy || !fleet.administrator} onClick={() => run(async () => { await mutation.mutateAsync({ transaction: removePasskeyTx(fleet.customerId, entries.data![index * 2], key) }); })}>Remove passkey</button></li>)}</ul>
    <p>Recovery: use another already enrolled passkey, then remove the lost key. Wallet-only reset is refused. Losing every required passkey permanently locks protected administration. Public credential metadata is stored in this browser; preserve it when moving browsers. Do not delete metadata for your only usable key.</p>
    {error && <p role="alert">{error}</p>}
  </section>;
}
