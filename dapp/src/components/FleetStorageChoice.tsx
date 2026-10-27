"use client";

import { formatToken } from "@/lib/network-costs";
import type { StorageView } from "@/lib/fleet-budget";

/**
 * Choose where an upload's Walrus storage comes from: the publisher (the
 * default), or the fleet's prepaid storage (SF-083), which keeps the blob
 * fleet-owned and asks the wallet to sign its registration and
 * certification. Disabled, with the reason, when fleet storage is unavailable.
 */
export function FleetStorageChoice({ checked, onChange, available, storage, reason, disabled, retention }: {
  retention?: { target: number; maxFee: string; setTarget: (value: number) => void; setMaxFee: (value: string) => void };
  checked: boolean; onChange: (checked: boolean) => void; available: boolean; storage?: StorageView; reason?: string; disabled?: boolean;
}) {
  return <div className="text-xs space-y-1">
    <label className="flex items-center gap-2">
      <input type="checkbox" checked={checked && available} disabled={!available || disabled} onChange={(e) => onChange(e.target.checked)} />
      Pay Walrus storage from the fleet&apos;s prepaid storage (fleet-owned blobs; your wallet signs registration and certification)
    </label>
    {available && storage && <p className="text-gray-500">Prepaid: {storage.poolSize.toString()} encoded bytes until Walrus epoch {storage.endEpoch ?? "?"}; {formatToken(storage.walBalance)} WAL for write fees.</p>}
    {retention && checked && <div>
      <label>Owner-authorized renewal target epoch <input type="number" min={1} className="border w-20" value={retention.target} onChange={(e) => retention.setTarget(Number(e.target.value))} /></label>
      <label>Maximum renewal WAL per blob <input className="border w-20" value={retention.maxFee} onChange={(e) => retention.setMaxFee(e.target.value)} /></label>
      <p>Every artifact and recipe uploaded here reserves this finite target atomically. Deposit enough protected WAL and execution SUI first. New uploads fail if the complete fleet closure or reserves are insufficient.</p>
    </div>}
    {!available && reason && <p className="text-gray-500">{reason} <a className="underline" href="/budget/">Create or fund fleet storage</a></p>}
  </div>;
}
