"use client";
import { useRef, useState } from 'react';
import { useCurrentAccount, useCurrentClient } from '@mysten/dapp-kit-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useFleetStorage } from '@/hooks/useFleetStorage';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { NETWORK } from '@/lib/constants';
import { DAY, storageSchedule, releaseWalrus, releaseObjects, periodEpochs, expiryTime, expiryWarning, requireDirectExtension, buildExtendFleetReleaseTx, buildExtendWalletReleaseTx } from '@/lib/release-storage';
import { useDeploymentStorage } from '@/hooks/useDeploymentStorage';
import { deploymentRecipeDigest } from '@/lib/release-recipe';
import { ConfidentialRecipeStorage } from './ConfidentialRecipeStorage';
import { fetchDeploymentVersion } from '@/lib/catalog';
import { formatToken } from '@/lib/network-costs';

export function ReleaseStorageStatus({ blobId, blobObjectId: _hint }: { blobId: string; blobObjectId?: string }) {
  const client = useCurrentClient(), account = useCurrentAccount(), fleet = useFleetStorage(), cache = useQueryClient();
  const execute = useFleetTransaction();
  const [days, setDays] = useState(7), [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const schedule = useQuery({ queryKey: ['release-schedule', NETWORK], queryFn: () => storageSchedule(client), staleTime: 60_000 });
  const owned = useQuery({ queryKey: ['release-objects', NETWORK, account?.address, fleet.target?.storageId], enabled: !!account,
    queryFn: () => releaseObjects(client, account!.address, fleet.target), refetchInterval: 60_000 });
  const status = useQuery({ queryKey: ['release-expiry', NETWORK, blobId], queryFn: async () => (await releaseWalrus(client)).getVerifiedBlobStatus({ blobId }), refetchInterval: 60_000 });
  const object = owned.data?.find(o => o.blobId === blobId);
  const end = status.data?.type === 'permanent' ? status.data.endEpoch : object?.endEpoch;
  const endMs = end !== undefined && schedule.data ? expiryTime(end, schedule.data) : undefined;
  const warning = expiryWarning(endMs);
  let epochs = 0;
  try { if (schedule.data) epochs = periodEpochs(days, schedule.data); } catch { /* invalid period disables action */ }
  const quote = useQuery({ queryKey: ['release-extension-price', NETWORK, object?.size, epochs], enabled: !!object && epochs > 0,
    queryFn: async () => (await (await releaseWalrus(client)).storageCost(object!.size, epochs)).storageCost });
  async function extend() {
    if (inFlight.current || !object || !schedule.data || !epochs || quote.data === undefined) return;
    inFlight.current = true; setBusy(true); setMessage('');
    try {
      // Refresh authority and expiry before building: stale display data never authorizes payment.
      const fresh = (await releaseObjects(client, account!.address, fleet.target)).find(o => o.id === object.id && o.blobId === blobId);
      requireDirectExtension(fresh?.retained);
      const s = await storageSchedule(client);
      const extra = periodEpochs(days, s);
      if (!fresh || fresh.endEpoch <= s.epoch) throw Error('Blob expired or ownership changed; restore verified bytes or reconnect its owner');
      if (fresh.endEpoch + extra > s.epoch + s.maxEpochs) throw Error('Extension exceeds the network storage horizon; choose fewer days');
      const tx = fresh.fleet ? await buildExtendFleetReleaseTx(client, fleet.target!, fresh.id, extra) : await buildExtendWalletReleaseTx(client, fresh.id, extra);
      const result = await execute.mutateAsync({ transaction: tx });
      setMessage(`Storage extension confirmed: ${result.digest}`);
      await Promise.all([cache.invalidateQueries({ queryKey: ['release-objects'] }), cache.invalidateQueries({ queryKey: ['release-expiry'] })]);
    } catch (e) { setMessage(String(e)); } finally { inFlight.current = false; setBusy(false); }
  }
  return <div className="text-xs space-y-1 break-words">
    <p>Storage expiry: {endMs === undefined ? status.isLoading ? 'checking…' : 'unknown' : `${new Date(endMs).toISOString().slice(0, 10)} (epoch ${end}) · projected from current epoch duration`}</p>
    {warning && <p className="text-amber-700" role="status">{warning}</p>}
    {(status.error || owned.error || schedule.error) && <p>Storage details unavailable: {String(status.error ?? owned.error ?? schedule.error)}</p>}
    {object?.retained && <p role="status">Scheduled retention is attached. Manage its renewal target on the Network budget page; direct extension is disabled to preserve prepaid storage.</p>}
    {object && !object.retained && <div><label>Extend storage by days <input className="border w-20 p-1" type="number" min="1" value={days} disabled={busy} onChange={e => setDays(Number(e.target.value))} /></label><p>{quote.data !== undefined ? `Estimated ${formatToken(quote.data)} WAL from ${object.fleet ? 'fleet funds' : 'your wallet'}, plus SUI gas` : 'Loading extension price…'}{schedule.data && end !== undefined && epochs > 0 ? ` · new projected expiry ${new Date(expiryTime(end + epochs, schedule.data)).toISOString().slice(0, 10)}` : ''}</p><button className="rounded border px-2 py-1" disabled={busy || !epochs || quote.data === undefined || !endMs || endMs <= Date.now()} onClick={extend}>{busy ? 'Extending…' : 'Extend storage'}</button></div>}
    {!object && !owned.isLoading && <p>Connect the storage owner or select its fleet to extend this blob.</p>}
    {message && <p role="status">{message}</p>}
  </div>;
}


export function ReleaseRecipeStorage({ recipeBlobId, recipeSha256, deploymentId }: { recipeBlobId: string; recipeSha256?: string; deploymentId?: string }) {
  const probe = useDeploymentStorage(recipeBlobId), client = useCurrentClient();
  const digest = useQuery({ queryKey: ['deployment-recipe-digest', NETWORK, deploymentId], enabled: !!deploymentId && !!probe.data?.confidential && !recipeSha256, queryFn: () => deploymentRecipeDigest(client, deploymentId!) });
  const sha256 = recipeSha256 ?? digest.data;
  return <div className="space-y-1"><p>Downloads: recipe {probe.data?.recipe ?? "checking"} · artifact {probe.data?.artifact ?? "checking"}</p><p>Recipe retention</p><ReleaseStorageStatus blobId={recipeBlobId} />
    {probe.data?.artifactBlobId ? <><p>Artifact retention</p><ReleaseStorageStatus blobId={probe.data.artifactBlobId} /></> : <p className="text-xs">Artifact expiry unknown{probe.data?.confidential ? ': confidential recipe requires authorized decryption to inspect its artifact reference.' : ': recipe could not be inspected.'}</p>}
    {digest.error && <p>Recipe digest unavailable: {String(digest.error)}</p>}{probe.data?.confidential && sha256 && <ConfidentialRecipeStorage blobId={recipeBlobId} sha256={sha256} />}
  </div>;
}
export function CatalogDeploymentStorage({ deploymentId }: { deploymentId: string }) {
  const client = useCurrentClient();
  const version = useQuery({ queryKey: ['deployment-storage-version', NETWORK, deploymentId], queryFn: () => fetchDeploymentVersion(client, deploymentId) });
  if (version.error) return <p role="status">Version storage unavailable: {String(version.error)}</p>;
  return <div>{version.isLoading ? 'Loading version retention…' : version.data?.manifests.map((m, i) => <div key={i}><p>{m.os}/{m.arch}</p><ReleaseRecipeStorage recipeBlobId={m.recipeBlobId} recipeSha256={m.recipeSha256} /></div>)}</div>;
}
