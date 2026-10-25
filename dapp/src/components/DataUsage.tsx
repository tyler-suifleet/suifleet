"use client";
import { useCurrentClient } from '@mysten/dapp-kit-react';
import { useQuery } from '@tanstack/react-query';
import { useFleetStorage } from '@/hooks/useFleetStorage';
import { useWalrusState } from './FleetBudget';
import { readDataMeter } from '@/lib/data-metering';
import { DATA_CONFIG } from '@/lib/fleet-data';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import { NETWORK } from '@/lib/constants';

const number = (value: bigint) => value.toLocaleString('en-US');
export function DataUsage() {
  const client = useCurrentClient();
  const { storage, budget, config, reason } = useFleetStorage();
  const schedule = useWalrusState(config);
  const matchesFleet = !DATA_CONFIG || (!!budget && !!storage && normalizeSuiAddress(budget.id) === DATA_CONFIG.budgetId && normalizeSuiAddress(storage.id) === DATA_CONFIG.storageId);
  const usage = useQuery({
    queryKey: ['data-meter', NETWORK, storage?.id, budget?.id, config?.storageOrigin ?? config?.packageId, schedule.data],
    enabled: matchesFleet && !!storage && !!budget && !!config && !!schedule.data,
    refetchInterval: 60000,
    queryFn: () => {
      const toMs = Date.now(), date = new Date(toMs);
      const fromMs = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1);
      return readDataMeter(client, storage!.id, budget!.id, config!.storageOrigin ?? config!.packageId, { ...schedule.data!, fromMs, toMs });
    },
  });
  const result = matchesFleet ? usage.data : undefined;
  return <section className="border rounded p-3 my-3 text-xs space-y-2">
    <h2 className="font-semibold">Fleet device-data usage</h2>
    <p>Authoritative storage records, refreshed every minute. Streams group all data kinds by uploading device; owner artifacts and recipes appear separately. Bytes are decimal bytes.</p>
    {(usage.error || schedule.error) && <p role="alert">Usage unavailable: {(usage.error ?? schedule.error)?.message}</p>}
    {!result && !usage.error && <p>{(!matchesFleet ? 'The connected owner budget does not match the configured data fleet.' : reason) ?? (schedule.error ? 'Walrus schedule unavailable.' : 'Loading storage usage…')}</p>}
    {result && !usage.error && <>
      <p>Fleet lifetime encoded writes: {number(result.writtenEncodedBytes)} bytes; currently registered and unexpired: {number(result.storedBytes)} logical bytes; held records: {number(result.retainedBytes)} logical bytes, including expired blobs.</p>
      <table className="w-full"><thead><tr className="text-left"><th>Stream (uploader)</th><th>Lifetime encoded bytes written</th><th>Unexpired logical bytes</th><th>Stored GB-month, month to date</th></tr></thead>
        <tbody>{result.streams.map(row => <tr key={row.device ?? 'owner'}><td className="break-all">{row.device ?? 'Owner releases (outside device allowance)'}</td><td>{number(row.writtenEncodedBytes)}</td><td>{number(row.storedBytes)}</td><td>{(Number(row.storedByteMs) / 1e9 / (365.25 / 12 * 86400000)).toFixed(6)}</td></tr>)}</tbody></table>
      <p>Stored byte-time covers only records still held by this fleet, clipped to this UTC month and their Walrus expiry. Released blobs retain lifetime write counters but their removed records cannot establish historical byte-time. Expiry dates project the current epoch schedule. Registration does not establish Walrus certification or physical availability. These figures are usage evidence, not a billing invoice; encoded lifetime writes include Walrus redundancy and must not be priced as logical monthly writes.</p>
    </>}
    <a className="underline" href="/settings/">Plan expected data volume, included allowance, cost, price and prepaid deposit</a>
  </section>;
}
