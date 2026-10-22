"use client";

import { formatToken } from "@/lib/network-costs";
import type { StorageView } from "@/lib/fleet-budget";

/**
 * Choose where an upload's Walrus storage comes from: the publisher (the
 * default), or the fleet's prepaid storage (SF-083), which keeps the blob
 * fleet-owned and asks the wallet to sign its registration and
 * certification. Disabled, with the reason, when fleet storage is unavailable.
 */
export function FleetStorageChoice({ checked, onChange, available, storage, reason, disabled }: {
  checked: boolean; onChange: (checked: boolean) => void; available: boolean; storage?: StorageView; reason?: string; disabled?: boolean;
}) {
  return <div className="text-xs space-y-1">
    <label className="flex items-center gap-2">
      <input type="checkbox" checked={checked && available} disabled={!available || disabled} onChange={(e) => onChange(e.target.checked)} />
      Pay Walrus storage from the fleet&apos;s prepaid storage (fleet-owned blobs; your wallet signs registration and certification)
    </label>
    {available && storage && <p className="text-gray-500">Prepaid: {storage.poolSize.toString()} encoded bytes until Walrus epoch {storage.endEpoch ?? "?"}; {formatToken(storage.walBalance)} WAL for write fees.</p>}
    {!available && reason && <p className="text-gray-500">{reason}</p>}
  </div>;
}
