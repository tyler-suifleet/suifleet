"use client";
import { useState, useRef, useEffect } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useQuery } from "@tanstack/react-query";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { normalizeId } from "@/lib/deployment-manifest";
import { readCustomerEntitlement, registryAdministrator, buildGrantDeviceLimitTx } from "@/lib/customer-entitlements";

export function DeviceEntitlements() {
  const account = useCurrentAccount(), client = useCurrentClient();
  const { fleet, refresh } = useFleetSelection();
  const mutation = useFleetTransaction();
  const [customerId, setCustomerId] = useState("");
  const [limit, setLimit] = useState("3");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const latestAccount = useRef(account?.address); latestAccount.current = account?.address;
  useEffect(() => () => { latestAccount.current = undefined; }, []);
  const [message, setMessage] = useState("");
  const admin = useQuery({ queryKey: ["entitlement-admin", account?.address], enabled: !!account, queryFn: () => registryAdministrator(client) });
  const isAdmin = !!account && admin.data === normalizeId(account.address);
  async function grant(event: React.FormEvent) {
    event.preventDefault(); if (inFlight.current) return; inFlight.current = true; setBusy(true); setMessage("");
    try {
      if (!account || await registryAdministrator(client) !== normalizeId(account.address)) throw Error("Only the registry administrator can grant device entitlements");
      const id = normalizeId(customerId);
      const current = await readCustomerEntitlement(client, id);
      const tx = buildGrantDeviceLimitTx(id, limit, current);
      if (latestAccount.current !== account.address) throw Error("Account changed");
      await mutation.mutateAsync({ transaction: tx });
      refresh(); setMessage(`Device limit saved for ${current.name}: ${limit}.`);
    } catch { setMessage("Device limit was not confirmed. Check the customer ID, registry administrator, active count and network, then refresh before retrying."); } finally { inFlight.current = false; setBusy(false); }
  }
  return <section className="space-y-4">
    <h1 className="text-xl font-semibold">Device entitlements</h1>
    <p>New customer accounts start with a device limit of zero. A registry administrator grants a limit here before the first device is registered. The limit is shared across all fleets in the customer account.</p>
    {fleet && <p>Customer: {fleet.customerName}<br />Customer ID: <code>{fleet.customerId}</code><br />{fleet.activeDevices.toString()} active devices / {fleet.deviceLimit.toString()} allowed.</p>}
    <p>To raise your limit, share your customer ID with the registry administrator and ask them to use Administration → Device entitlements. Network budget deposits do not increase device capacity. Subscriptions are not yet available.</p>
    {!account ? <p>Connect your wallet to verify grant authority.</p> : admin.isError ? <p role="alert">Registry authority could not be verified. Refresh before granting.</p> : !isAdmin ? <p>{admin.isPending ? "Verifying registry authority…" : "Your connected account cannot grant entitlements. Connect the registry administrator account to grant a limit."}</p> :
      <form onSubmit={grant} className="space-y-3">
        <p>You are the registry administrator. Grants preserve the current usage allowance and period; they do not fund device gas or buy a subscription.</p>
        <label className="block">Customer ID<input className="block border p-2 w-full" required value={customerId} onChange={e => setCustomerId(e.target.value)} /></label>
        <label className="block">Device limit<input className="block border p-2" required inputMode="numeric" value={limit} onChange={e => setLimit(e.target.value)} /></label>
        <button disabled={busy} className="border rounded px-4 py-2" type="submit">{busy ? "Saving…" : "Grant device limit"}</button>
      </form>}
    {message && <p role="status">{message}</p>}
  </section>;
}
