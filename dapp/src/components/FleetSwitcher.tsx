"use client";
import { useEffect, useRef, useState } from "react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildCreateCustomerTx, buildCreateFleetTx } from "@/lib/customer-fleets";
export function FleetSwitcher() {
  const selection = useFleetSelection();
  return <FleetCreation key={selection.identity} />;
}
function FleetCreation() {
  const selection = useFleetSelection(), account = useCurrentAccount(), mutation = useFleetTransaction();
  const [creating, setCreating] = useState(false), [name, setName] = useState(""), [error, setError] = useState("");
  const inFlight = useRef(false);
  const [uncertain, setUncertain] = useState(false);
  const knownCount = useRef(selection.fleets.length);
  useEffect(() => {
    if (selection.fleets.length > knownCount.current) setUncertain(false);
    knownCount.current = selection.fleets.length;
  }, [selection.fleets.length]);
  if (!selection.supported || !account) return null;
  async function create() {
    if (inFlight.current || uncertain) return;
    inFlight.current = true;
    try {
      setError("");
      const transaction = selection.fleet ? buildCreateFleetTx(selection.fleet.customerId, name) : buildCreateCustomerTx("My customer");
      await mutation.mutateAsync({ transaction, onProgress: phase => { if (phase === "wallet") setUncertain(true); }, onSubmitted: () => setUncertain(true), onDefiniteFailure: () => setUncertain(false) }); setUncertain(true); selection.refresh(); setCreating(false); setName("");
    } catch (e) { setError((e instanceof Error ? e.message : "Fleet creation failed") + ". If you cancelled or rejected approval, no fleet was created. Check the required network and account SUI balance. If submission may have reached the network, check account transaction history and refresh fleets before trying again."); } finally { inFlight.current = false; }
  }
  return <section aria-label="Fleet selection" className="mb-4 rounded border p-3 bg-white text-sm">
    {selection.loading ? <p role="status">Loading fleets…</p> : selection.error ? <p role="alert">Could not verify fleets: {selection.error.message} <button onClick={selection.refresh}>Retry</button></p> : <>
      {selection.stale && <p role="alert">The selected fleet is no longer available. Choose another authorized fleet or contact a customer administrator.</p>}
      <label htmlFor="fleet-switcher" className="mr-2 font-medium">Fleet</label>
      <select id="fleet-switcher" value={selection.fleet?.id ?? ""} onChange={e => selection.select(e.target.value)}>
        <option value="" disabled>{selection.fleets.length ? "Choose a fleet" : "No fleets yet"}</option>
        {selection.fleets.map((f, i) => <option key={f.id} value={f.id}>{f.customerName} / {f.name}{selection.fleets.some(other => other.id !== f.id && other.name === f.name) ? ` (${i + 1})` : ""}</option>)}
      </select>
      {((!selection.fleets.length && !selection.stale) || selection.fleet?.administrator) && <button className="ml-3 underline" disabled={mutation.isPending || uncertain} onClick={() => setCreating(!creating)}>{selection.fleets.length ? "Create fleet" : "Create default fleet"}</button>}
      {selection.fleet && <details className="mt-2"><summary>Fleet details</summary><p>{selection.fleet.id}</p><p>Customer devices: {String(selection.fleet.activeDevices)} / {String(selection.fleet.deviceLimit)}</p><p>Customer upload allowance: {String(selection.fleet.used)} / {String(selection.fleet.allowance)} encoded bytes</p></details>}
      {creating && <div className="mt-3 space-y-2"><h3 className="font-semibold">Review fleet creation</h3><p>{selection.fleet ? "Create another fleet in this customer account." : "Create a customer account and its default fleet, a workspace for your devices."} This requires an account-paid network fee. Your wallet shows the fee before approval.</p><p>A new customer starts with zero device capacity. Creating a fleet does not buy capacity or fund device reporting. Next, check Device capacity for subscription availability, then Network budget for separate device reporting funds and spending limits. If checkout is unavailable, contact the operator; sending more SUI does not unlock capacity. No fees are silently sponsored.</p><form onSubmit={e => { e.preventDefault(); void create(); }}>{selection.fleet && <label>Fleet name <input value={name} onChange={e => setName(e.target.value)} required maxLength={80}/></label>}<button disabled={mutation.isPending || uncertain}>Create</button><button type="button" disabled={mutation.isPending} onClick={() => setCreating(false)}>Cancel</button></form></div>}
    </>}
    {mutation.isPending && <p role="status">Creating fleet… Check your wallet for approval, then wait for network confirmation. Do not submit again.</p>}
    {uncertain && !mutation.isPending && <p role="alert">Waiting for the fleet list to confirm the result. Check account transaction history and refresh fleets. Reload only after checking the result; do not create a duplicate.</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
