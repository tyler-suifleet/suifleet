"use client";
import { useState } from "react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildCreateCustomerTx, buildCreateFleetTx } from "@/lib/customer-fleets";
export function FleetSwitcher() {
  const selection = useFleetSelection(), account = useCurrentAccount(), mutation = useFleetTransaction();
  const [creating, setCreating] = useState(false), [name, setName] = useState(""), [error, setError] = useState("");
  if (!selection.supported || !account) return null;
  async function create() {
    try {
      setError("");
      const transaction = selection.fleet ? buildCreateFleetTx(selection.fleet.customerId, name) : buildCreateCustomerTx("My customer");
      await mutation.mutateAsync({ transaction }); selection.refresh(); setCreating(false); setName("");
    } catch (e) { setError(e instanceof Error ? e.message : "Fleet creation failed"); }
  }
  return <section aria-label="Fleet selection" className="mb-4 rounded border p-3 bg-white text-sm">
    {selection.loading ? <p role="status">Loading fleets…</p> : selection.error ? <p role="alert">Could not verify fleets: {selection.error.message} <button onClick={selection.refresh}>Retry</button></p> : <>
      {selection.stale && <p role="alert">The selected fleet is no longer available. Choose another authorized fleet or contact a customer administrator.</p>}
      <label htmlFor="fleet-switcher" className="mr-2 font-medium">Fleet</label>
      <select id="fleet-switcher" value={selection.fleet?.id ?? ""} onChange={e => selection.select(e.target.value)}>
        <option value="" disabled>{selection.fleets.length ? "Choose a fleet" : "No fleets yet"}</option>
        {selection.fleets.map((f, i) => <option key={f.id} value={f.id}>{f.customerName} / {f.name}{selection.fleets.some(other => other.id !== f.id && other.name === f.name) ? ` (${i + 1})` : ""}</option>)}
      </select>
      {((!selection.fleets.length && !selection.stale) || selection.fleet?.administrator) && <button className="ml-3 underline" disabled={mutation.isPending} onClick={() => selection.fleets.length ? setCreating(!creating) : void create()}>{selection.fleets.length ? "Create fleet" : "Create default fleet"}</button>}
      {selection.fleet && <details className="mt-2"><summary>Fleet details</summary><p>{selection.fleet.id}</p><p>Customer devices: {String(selection.fleet.activeDevices)} / {String(selection.fleet.deviceLimit)}</p><p>Customer upload allowance: {String(selection.fleet.used)} / {String(selection.fleet.allowance)} encoded bytes</p></details>}
      {creating && <form onSubmit={e => { e.preventDefault(); void create(); }}><label>Fleet name <input value={name} onChange={e => setName(e.target.value)} required maxLength={80}/></label><button disabled={mutation.isPending}>Create</button></form>}
    </>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
