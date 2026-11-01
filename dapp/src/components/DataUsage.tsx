"use client";
import { formatStorageQueryError } from "@/lib/storage-errors";
import { useCurrentClient } from '@mysten/dapp-kit-react';
import { useQuery } from '@tanstack/react-query';
import { useFleetStorage } from '@/hooks/useFleetStorage';
import { useWalrusState } from './FleetBudget';
import { readDataMeter } from '@/lib/data-metering';
import { useFleetDataConfig } from '@/hooks/useFleetDataConfig';
import { useFleetSelection } from '@/hooks/useFleetSelection';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import { NETWORK } from '@/lib/constants';

const number = (value: bigint) => value.toLocaleString('en-US');
export function DataUsage() {
  const client = useCurrentClient();
  const DATA_CONFIG = useFleetDataConfig();
  const selection = useFleetSelection();
  const { storage, budget, config, reason } = useFleetStorage();
  const schedule = useWalrusState(config);
  const matchesFleet = !DATA_CONFIG || (!!budget && !!storage && normalizeSuiAddress(budget.id) === DATA_CONFIG.budgetId && normalizeSuiAddress(storage.id) === DATA_CONFIG.storageId);
  const usage = useQuery({
    queryKey: ['data-meter', NETWORK, selection.identity, storage?.id, budget?.id, config?.storageOrigin ?? config?.packageId, schedule.data],
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
    <p>Usage refreshes every minute. Device uploads are grouped by device; software releases and installation instructions are listed separately.</p>
    {(usage.error || schedule.error) && <p role="alert">{formatStorageQueryError(usage.error ?? schedule.error, "Storage usage")}</p>}
    {!result && !usage.error && <p>{(budget && storage && !matchesFleet ? 'The connected owner budget does not match the configured data fleet.' : reason) ?? (schedule.error ? 'Storage availability information is unavailable.' : 'Loading storage usage…')}</p>}
    {result && !usage.error && <>
      <p>Total uploaded bytes (including storage redundancy): {number(result.writtenEncodedBytes)} bytes; data within its registered storage period: {number(result.storedBytes)} bytes; retained records: {number(result.retainedBytes)} bytes, including expired uploads.</p>
      <table className="w-full"><thead><tr className="text-left"><th>Uploaded by</th><th>Total uploaded bytes (includes redundancy)</th><th>Bytes within storage period</th><th>Storage used this month (GB-month)</th></tr></thead>
        <tbody>{result.streams.map(row => <tr key={row.device ?? 'owner'}><td className="break-all">{row.device ?? 'Owner releases (outside device allowance)'}</td><td>{number(row.writtenEncodedBytes)}</td><td>{number(row.storedBytes)}</td><td>{(Number(row.storedByteMs) / 1e9 / (365.25 / 12 * 86400000)).toFixed(6)}</td></tr>)}</tbody></table>
      <details><summary>How storage usage is calculated</summary><p>Stored byte-time covers only records still held by this fleet, clipped to this UTC month and their Walrus expiry. Released blobs retain lifetime write counters but their removed records cannot establish historical byte-time. Expiry dates project the current epoch schedule. Registration does not establish Walrus certification or physical availability. These figures are usage evidence, not a billing invoice; encoded lifetime writes include Walrus redundancy and must not be priced as logical monthly writes.</p></details>
    </>}
    <a className="underline" href="/settings/">Plan expected data volume, included allowance, cost, price and prepaid deposit</a>
  </section>;
}
