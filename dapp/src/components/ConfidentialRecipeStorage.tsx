"use client";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useEffect, useRef, useState } from 'react';
import { useCurrentClient, useCurrentNetwork, useDAppKit } from '@mysten/dapp-kit-react';
import { SessionKey } from '@mysten/seal';
import { requireManifest, verifyDeployment } from '@/lib/deployment-manifest';
import { sealNamespace } from '@/lib/access-policy';
import { SEAL_SETTINGS } from '@/lib/seal';
import { blobDownloadUrl } from '@/lib/walrus';
import { inspectConfidentialRecipe, boundedRecipeBytes } from '@/lib/release-recipe';
import { ReleaseStorageStatus } from './ReleaseStorageStatus';

export function ConfidentialRecipeStorage({ blobId, sha256 }: { blobId: string; sha256: string }) {
  const account = useCurrentAccount(), client = useCurrentClient(), wallet = useDAppKit(), network = useCurrentNetwork();
  const [artifact, setArtifact] = useState(''), [busy, setBusy] = useState(false), [message, setMessage] = useState('');
  const inFlight = useRef(false), visit = useRef(0);
  useEffect(() => { visit.current++; setArtifact(''); setBusy(false); setMessage(''); return () => { visit.current++; }; }, [account?.address, network, client, blobId, sha256]);
  async function inspect() {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setMessage(''); const captured = visit.current;
    try {
      if (!account || !SEAL_SETTINGS) throw Error('Connect an authorized account and configure Seal providers');
      const check = async () => {
        const manifest = requireManifest(); await verifyDeployment(client);
        const current = wallet.stores.$connection.get().account;
        if (captured !== visit.current || current?.address !== account.address || !current.chains.includes(`sui:${manifest.network}`)
          || wallet.stores.$currentNetwork.get() !== manifest.network) throw Error('Wallet account, network or view changed');
        return { manifest, current };
      };
      const { manifest, current } = await check();
      const session = await SessionKey.create({ address: account.address, packageId: sealNamespace(), ttlMin: 5, suiClient: client });
      const signed = await wallet.signPersonalMessage({ message: session.getPersonalMessage(), account: current, network: manifest.network });
      await session.setPersonalMessageSignature(signed.signature); await check();
      const response = await fetch(blobDownloadUrl(blobId), { signal: AbortSignal.timeout(30_000) });
      if (!response.ok) throw Error(`Recipe download failed (${response.status})`);
      // Encrypted recipe data only. Decrypted recipe bytes are wiped by the reader.
      const bytes = await boundedRecipeBytes(response);
      let id: string;
      try { id = await inspectConfidentialRecipe(client, bytes, sha256, SEAL_SETTINGS, session); } finally { bytes.fill(0); }
      await check();
      if (session.isExpired()) throw Error('Recipe session expired');
      setArtifact(id);
    } catch (e) { if (captured === visit.current) setMessage(String(e)); }
    finally { inFlight.current = false; if (captured === visit.current) setBusy(false); }
  }
  return <div className="text-xs space-y-1"><button className="border rounded px-2 py-1" disabled={busy || !account || !SEAL_SETTINGS} onClick={inspect}>{busy ? 'Checking authorized recipe…' : 'Show confidential artifact storage'}</button>
    <p>Uses a five-minute signed session and the release access policy. Decrypted recipe contents and session keys are kept in memory only.</p>
    {message && <p role="status">{message}</p>}{artifact && <ReleaseStorageStatus blobId={artifact} />}</div>;
}
