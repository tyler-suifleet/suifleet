"use client";
import type { useReleasePeriod } from '@/hooks/useReleasePeriod';
import { DAY } from '@/lib/release-storage';
import { formatToken } from '@/lib/network-costs';
export function ReleasePeriod({ period, disabled, fleetPaid = false }: { period: ReturnType<typeof useReleasePeriod>; disabled?: boolean; fleetPaid?: boolean }) {
  const end = fleetPaid && period.schedule && period.fleet.storage?.endEpoch ? Math.max(period.expiry ?? 0, period.schedule.firstEpochStartMs + period.fleet.storage.endEpoch * period.schedule.epochDurationMs) : period.expiry;
  return <div className="rounded border p-3 text-sm space-y-2">
    <label>Storage period (days) <input aria-label="Storage period (days)" className="border rounded p-1 w-24" type="number" min="0.01" step="any" value={period.days} disabled={disabled} onChange={e => period.setDays(Number(e.target.value))} /></label>
    {end && <p>Projected expiry: {new Date(end).toISOString().slice(0, 10)} · rounded up to {period.epochs} whole Walrus epoch(s){period.schedule ? ` (${period.epochs * period.schedule.epochDurationMs / DAY} days)` : ''}. Epoch durations can change.</p>}
    <p>{period.price !== undefined ? `Estimated storage and write price for selected artifact bytes: ${formatToken(period.price)} WAL.` : 'Choose artifacts to estimate the WAL storage price.'} Recipe/encryption overhead and SUI transaction fees are additional. {fleetPaid ? 'Existing prepaid capacity is used first; extra retention is charged from fleet WAL. A longer prepaid period is preserved.' : 'The public publisher currently pays storage; your wallet receives newly created blob objects. Publisher sponsorship and availability are not guaranteed.'}</p>
    {period.error && <p role="alert">Storage quote unavailable: {period.error}</p>}
  </div>;
}
