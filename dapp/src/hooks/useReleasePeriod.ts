"use client";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useState } from 'react';
import { useCurrentClient } from '@mysten/dapp-kit-react';
import { useQuery } from '@tanstack/react-query';
import { useFleetStorage, useFleetBlobStore } from './useFleetStorage';
import { fetchFleetSettings } from '@/lib/fleet-settings';
import { WALRUS_EPOCHS, NETWORK } from '@/lib/constants';
import { DAY, periodEpochs, expiryTime, storageSchedule, releaseWalrus, buildExtendFleetReleaseTx, requireFleetUploadPeriod } from '@/lib/release-storage';
import { uploadBlob, type BlobStore } from '@/lib/walrus';
import { useFleetTransaction } from './useFleetTransaction';
import { formatStorageQueryError } from '@/lib/storage-errors';

export function useReleasePeriod(sizes: number[]) {
  const client = useCurrentClient(), account = useCurrentAccount();
  const fleet = useFleetStorage(), prepaid = useFleetBlobStore(), execute = useFleetTransaction();
  const [daysOverride, setDays] = useState<number>();
  const schedule = useQuery({ queryKey: ['release-schedule', NETWORK], queryFn: () => storageSchedule(client), staleTime: 60_000 });
  const settings = useQuery({ queryKey: ['release-defaults', NETWORK, fleet.budget?.id], enabled: !!fleet.budget,
    queryFn: () => fetchFleetSettings(client, fleet.budget!.id) });
  const s = schedule.data;
  const days = daysOverride ?? (settings.data?.archiveRetentionEpochs ?? WALRUS_EPOCHS) * (s?.epochDurationMs ?? DAY) / DAY;
  let epochs = 0, error: string | undefined;
  try { if (s) epochs = periodEpochs(days, s); } catch (e) { error = String(e); }
  const quote = useQuery({ queryKey: ['release-price', NETWORK, epochs, sizes], enabled: epochs > 0 && sizes.length > 0 && sizes.every(n => n > 0),
    queryFn: async () => { const w = await releaseWalrus(client); const costs = await Promise.all(sizes.map(n => w.storageCost(n, epochs))); return costs.reduce((sum, c) => sum + c.totalCost, BigInt(0)); } });
  const publisherStore: BlobStore = async (body, progress) => {
    if (!s || error || !epochs || !account) throw Error('Load a valid storage period and connect your wallet before uploading');
    const current = await storageSchedule(client);
    const requested = periodEpochs(days, current);
    const stored = await uploadBlob(body, 'application/octet-stream', account.address, progress, requested);
    if (stored.endEpoch === undefined || stored.endEpoch < current.epoch + requested) throw Error('Publisher storage does not cover the chosen period; version was not registered');
    return stored;
  };
  const fleetStore: BlobStore = async (body, progress) => {
    if (!s || error || !epochs || !fleet.target || !prepaid.store) throw Error('Fleet storage or storage period unavailable');
    // A prepaid pool can have a longer period than requested; it cannot shorten a blob.
    const current = await storageSchedule(client);
    const targetEnd = current.epoch + periodEpochs(days, current);
    requireFleetUploadPeriod(!!prepaid.retention, prepaid.storage?.endEpoch, targetEnd);
    const blob = await prepaid.store(body, progress);
    if (blob.endEpoch === undefined) throw Error('Fleet registration omitted storage expiry');
    if (blob.endEpoch < targetEnd) {
      await execute.mutateAsync({ transaction: await buildExtendFleetReleaseTx(client, fleet.target, blob.blobObjectId!, targetEnd - blob.endEpoch) });
      blob.endEpoch = targetEnd;
    }
    return blob;
  };
  return { days, setDays, epochs, schedule: s, expiry: s && epochs ? expiryTime(s.epoch + epochs, s) : undefined,
    price: quote.data, error: error ?? (fleet.budgetError ? formatStorageQueryError(fleet.budgetError, 'Fleet budget') :
      schedule.error ? formatStorageQueryError(schedule.error, 'Storage schedule') :
      settings.error ? formatStorageQueryError(settings.error, 'Storage defaults') :
      quote.error ? formatStorageQueryError(quote.error, 'Storage price') : undefined),
    prepaid, loading: !s || fleet.loading || settings.isLoading || quote.isLoading, publisherStore, fleetStore, fleet };
}
