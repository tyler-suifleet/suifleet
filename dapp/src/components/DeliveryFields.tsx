"use client";

import Link from "next/link";
import type { AccessPolicy } from "@/lib/access-policy";
import type { Delivery } from "@/lib/delivery";
import { SEAL_SETTINGS } from "@/lib/seal";

/** Confidential (default) or explicitly public delivery for one release. */
export function DeliveryFields({ value, onChange, policies, disabled }: {
  value: Delivery;
  onChange: (value: Delivery) => void;
  policies: AccessPolicy[];
  disabled?: boolean;
}) {
  return (
    <fieldset className="border border-gray-200 rounded p-3 space-y-2" disabled={disabled}>
      <legend className="text-xs font-medium text-gray-600 px-1">Delivery</legend>
      <label className="flex items-start gap-2 text-sm">
        <input type="radio" name="delivery" checked={value.mode === "confidential"} onChange={() => onChange({ ...value, mode: "confidential" })} />
        <span>
          <strong>Confidential</strong> — encrypted with Seal in this browser before upload. Only accounts, devices and groups
          granted by the access policy can obtain keys.
        </span>
      </label>
      {value.mode === "confidential" && (
        <div className="pl-6 space-y-1">
          {!SEAL_SETTINGS && <p role="alert" className="text-xs text-red-600">Seal key servers are not configured for this frontend; confidential uploads are unavailable.</p>}
          <label className="block text-xs text-gray-600">
            Access policy{" "}
            <select value={value.policyId} onChange={e => onChange({ ...value, policyId: e.target.value })}>
              <option value="">Choose a policy…</option>
              {policies.map(p => <option key={p.objectId} value={p.objectId}>{p.name} · epoch {p.keyEpoch}</option>)}
            </select>
          </label>
          {policies.length === 0 && <p className="text-xs text-gray-500">You administer no access policies yet. <Link href="/access" className="text-[#4DA2FF]">Create one</Link>.</p>}
        </div>
      )}
      <label className="flex items-start gap-2 text-sm">
        <input type="radio" name="delivery" checked={value.mode === "public"} onChange={() => onChange({ ...value, mode: "public", publicAcknowledged: false })} />
        <span><strong>Public (unencrypted)</strong> — anyone can read it from Walrus. Only for public or open-source content.</span>
      </label>
      {value.mode === "public" && (
        <label className="pl-6 flex items-start gap-2 text-xs text-amber-700">
          <input type="checkbox" checked={value.publicAcknowledged} onChange={e => onChange({ ...value, publicAcknowledged: e.target.checked })} />
          I confirm this artifact and recipe contain nothing confidential and may be published unencrypted.
        </label>
      )}
    </fieldset>
  );
}
