"use client";
import type { useReleasePeriod } from '@/hooks/useReleasePeriod';
import { storageRemaining } from '@/lib/release-storage';
import { formatToken } from '@/lib/network-costs';
export function ReleasePeriod({ period, disabled, fleetPaid = false }: { period: ReturnType<typeof useReleasePeriod>; disabled?: boolean; fleetPaid?: boolean }) {
  const endEpoch = period.schedule && period.epochs > 0 ? Math.max(period.schedule.epoch + period.epochs, fleetPaid ? period.fleet.storage?.endEpoch ?? 0 : 0) : undefined;
  const remaining = storageRemaining(endEpoch,period.schedule);
  return <div className="rounded border p-3 text-sm space-y-2">
    <label>Storage period (days) <input aria-label="Storage period (days)" className="border rounded p-1 w-24" type="number" min="0.01" step="any" value={period.days} disabled={disabled} onChange={e => period.setDays(Number(e.target.value))} /></label>
    {remaining && <p>{remaining}. The current storage period may already be in progress; timing can change. A calendar expiry date is not verified.</p>}
    {period.schedule && endEpoch !== undefined && <details><summary>Storage period details</summary><p>Current network period: {period.schedule.epoch}. Requested whole periods: {period.epochs}. Stored through period: {endEpoch}. Longer prepaid coverage is preserved.</p></details>}
    <p>{period.price !== undefined ? `Estimated storage and write price for selected artifact bytes: ${formatToken(period.price)} WAL.` : 'Choose artifacts to estimate the WAL storage price.'} Recipe/encryption overhead and SUI transaction fees are additional. {fleetPaid ? 'Existing prepaid capacity is used first; extra retention is charged from fleet WAL. A longer prepaid period is preserved.' : 'The public publisher currently pays storage; your wallet receives newly created blob objects. Publisher sponsorship and availability are not guaranteed.'}</p>
    {period.error && <p role="alert">Storage quote unavailable: {period.error}</p>}
    <button type="button" className="rounded border px-3 py-2" disabled={disabled || period.refreshing} onClick={() => void period.refresh()}>{period.refreshing ? 'Refreshing storage quote…' : 'Refresh storage quote'}</button>
    {period.refreshing && <p role="status">Checking the storage period, settings and price. Your selected files stay in place; no upload or payment is started.</p>}
  </div>;
}
