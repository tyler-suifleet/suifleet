"use client";
import { PageHeader, InfoCard } from "@/components/ui";
import { useState } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useDevices } from "@/hooks/useDevices";
import { buildCredentialTx, buildReassignDeviceTx } from "@/lib/customer-fleets";
import type { Transaction } from "@mysten/sui/transactions";
export function CustomerAccess() {
  const selection = useFleetSelection(), client = useCurrentClient(), mutation = useFleetTransaction();
  const { data: devices = [] } = useDevices();
  const [wallet, setWallet] = useState(""), [admin, setAdmin] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [device, setDevice] = useState(""), [destination, setDestination] = useState("");
  const fleet = selection.fleet;
  if (!fleet) return null;
  async function submit(build: () => Transaction | Promise<Transaction>) {
    try { setError(""); setMessage(""); const result = await mutation.mutateAsync({ transaction: await build() }); selection.refresh(); setMessage(`Confirmed: ${result.digest}`); }
    catch (e) { setError(e instanceof Error ? e.message : "Customer administration failed"); }
  }
  return <div className="max-w-3xl space-y-5"><PageHeader title="Administration" subtitle="Choose who can manage your organization and move devices between its fleets." />
    <InfoCard title="Account access applies across this organization"><p>{fleet.customerName} keeps the same identity, fleets, device allowance and upload usage when accounts change. Administrators can manage all its fleets. Read-only accounts can view inventory. Logs and component access also follow their separate access policies.</p></InfoCard>
    {fleet.administrator && <>
      <form className="grid gap-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm" onSubmit={e => { e.preventDefault(); void submit(() => buildCredentialTx(fleet.customerId, wallet, true, admin)); }}>
        <label>Account address <input required className="border p-2" value={wallet} onChange={e => setWallet(e.target.value)} placeholder="0x…"/></label>
        <label><input type="checkbox" checked={admin} onChange={e => setAdmin(e.target.checked)}/> Administrator</label><button disabled={mutation.isPending}>Grant account access</button>
      </form>
      <details><summary>Accounts with access</summary><ul>{fleet.credentials.map(c => <li key={c.wallet} className="break-all">{c.wallet} · {c.administrator ? "Administrator" : "Read-only"} <button className="underline" disabled={mutation.isPending} onClick={() => void submit(() => buildCredentialTx(fleet.customerId, c.wallet, false, false))}>Revoke</button></li>)}</ul><p>Authorize and verify a replacement administrator before revoking the previous one. The contract preserves at least one administrator.</p></details>
      <section className="border rounded p-4"><h2 className="font-semibold">Move a device to another fleet</h2><p>Remove its group and direct policy grants first. Unused activity funds return to the current fleet budget. Its key, previous deployments and archives are preserved. Fund a reserve in the destination before it needs to pay more network fees. Switching the fleet menu never moves a device.</p>
        <form onSubmit={e => { e.preventDefault(); const target = selection.fleets.find(f => f.id === destination); if (target && device && confirm("Move this device and return unused activity funds to its current fleet budget?")) void submit(() => buildReassignDeviceTx(client, fleet, target, device)); }}>
          <label>Device <select required value={device} onChange={e => setDevice(e.target.value)}><option value="">Choose device</option>{devices.map(d => <option key={d.objectId} value={d.objectId}>{d.name}</option>)}</select></label>
          <label>Destination fleet <select required value={destination} onChange={e => setDestination(e.target.value)}><option value="">Choose destination</option>{selection.fleets.filter(f => f.customerId === fleet.customerId && f.id !== fleet.id).map((f, i) => <option key={f.id} value={f.id}>{f.name} ({i + 1})</option>)}</select></label><button disabled={mutation.isPending}>Move device</button>
        </form>
      </section>
    </>}
    {!fleet.administrator && <p>Ask a customer administrator to change credentials or device assignments.</p>}
    {error && <p role="alert">{error}</p>}{message && <p role="status" className="break-all">{message}</p>}
  </div>;
}
