"use client";
import { useState, useRef, useEffect } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useQuery } from "@tanstack/react-query";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import Link from "next/link";
import { MANIFEST } from "@/lib/deployment-manifest";
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
  const subscriptionSupport = !!MANIFEST?.type_origins["subscription::Account"] && !!MANIFEST?.subscription_coins;
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
    <h1 className="text-xl font-semibold">Device capacity</h1>
    <p>Device capacity is the number of active devices your organization can manage across all its fleets. A new account may need capacity enabled before its first device can be registered. Adding funds to the network budget does not add device capacity.</p>
    {fleet && <p>Organization: {fleet.customerName}<br />Organization ID: <code>{fleet.customerId}</code><br />{fleet.activeDevices.toString()} active devices / {fleet.deviceLimit.toString()} allowed.</p>}
    <p>{subscriptionSupport ? <>Open <Link className="text-blue-700 underline" href="/billing">Plans and billing</Link> to review available capacity offers and your subscription. If no suitable offer is listed, contact your service administrator.</> : <>Plan checkout is not enabled in this workspace. Share your organization ID with the service administrator—the person who manages this deployment—and ask them to enable or raise your device limit.</>}</p>
    {!account ? <p>Connect your wallet to verify grant authority.</p> : admin.isError ? <p role="alert">Administrator permissions could not be verified. Refresh before changing a limit.</p> : !isAdmin ? <p>{admin.isPending ? "Checking administrator permissions…" : "Your account can view capacity but cannot change it here. A service administrator must approve changes."}</p> :
      <form onSubmit={grant} className="space-y-3">
        <p>Service administrator tools: change a device limit without changing the current data allowance or period. This does not add network funds or buy a subscription.</p>
        <label className="block">Organization ID<input className="block border p-2 w-full" required value={customerId} onChange={e => setCustomerId(e.target.value)} /></label>
        <label className="block">Device limit<input className="block border p-2" required inputMode="numeric" value={limit} onChange={e => setLimit(e.target.value)} /></label>
        <button disabled={busy} className="border rounded px-4 py-2" type="submit">{busy ? "Saving…" : "Grant device limit"}</button>
      </form>}
    {message && <p role="status">{message}</p>}
  </section>;
}
