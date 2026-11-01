"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { toHex, normalizeSuiAddress } from "@mysten/sui/utils";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { fetchOrganizationSecurity, SECURITY_ACTIONS } from "@/lib/security-policy";
import { decisionTx, executePolicyTx, fetchRequests, policyValues, secondPersonSupported } from "@/lib/second-person";

export function SecondPersonApprovals() {
  const selection = useFleetSelection(), client = useCurrentClient(), account = useCurrentAccount(), mutation = useFleetTransaction();
  const fleet = selection.fleet;
  const [error, setError] = useState("");
  const query = useQuery({ queryKey: ["second-person-pending", selection.identity, fleet?.customerId], enabled: !!fleet && secondPersonSupported(), queryFn: async () => ({ requests: await fetchRequests(client, fleet!.customerId), policy: await fetchOrganizationSecurity(client, fleet!.customerId) }), refetchInterval: 15000 });
  if (!fleet || !secondPersonSupported()) return null;
  async function act(work: () => Promise<Parameters<typeof mutation.mutateAsync>[0]["transaction"]> | Parameters<typeof mutation.mutateAsync>[0]["transaction"]) {
    try { setError(""); await mutation.mutateAsync({ transaction: await work() }); await query.refetch(); } catch (e) { setError(e instanceof Error ? e.message : "Approval failed"); }
  }
  return <section className="space-y-3 rounded border p-4" aria-label="Pending approvals"><h2>Pending approvals</h2>
    <p>Review every argument before approving. Approval authorizes these exact bytes. A different current administrator must approve; only the proposer can execute. The network clock decides expiry. Rejection removes a request; cancellation needs no factors.</p>
    {query.isPending && <p>Reading requests…</p>}{query.error && <p role="alert">{query.error.message}</p>}
    {query.data?.requests.length === 0 && <p>No pending requests.</p>}
    {query.data?.requests.map(request => {
      const mine = !!account && normalizeSuiAddress(account.address) === normalizeSuiAddress(request.proposer);
      const stale = request.generation !== query.data.policy?.generation;
      const expired = request.expires <= BigInt(Date.now());
      const enrollment = request.operation === "configure_second_person" && request.arguments.length === 2;
      let values: number[] | undefined;
      try { values = policyValues(request); } catch { /* Still show the raw request; never execute an unsupported schema. */ }
      return <article key={request.id} className="rounded border p-3 space-y-2 break-all"><h3>{request.operation}</h3>
        <p>Request {request.id}. Proposed by {request.proposer}. Expires {new Date(Number(request.expires)).toISOString()}. {stale ? "Stale: propose again." : expired ? "Expired." : request.approved ? "Approved by another credential." : "Awaiting another administrator."}</p>
        {enrollment && <p>Configure second-person approval for this organization. All different current administrators are eligible; policy requirements remain unchanged until saved.</p>}
        {values && <ul>{values.map((value, i) => <li key={i}>{SECURITY_ACTIONS[i]}: {value & 4 ? "Second-person approval" : "Off"}</li>)}</ul>}
        <details><summary>Exact action arguments (hex)</summary>{request.arguments.map((arg, i) => <p key={i}>Argument {i}: {toHex(Uint8Array.from(arg))}</p>)}</details>
        {fleet.administrator && <div className="flex flex-wrap gap-3">
          {!mine && <><button disabled={mutation.isPending || expired || stale || request.approved} onClick={() => act(() => decisionTx(fleet.customerId, request.id, "approve"))}>Approve exact action</button><button disabled={mutation.isPending} onClick={() => act(() => decisionTx(fleet.customerId, request.id, "reject"))}>Reject</button></>}
          <button disabled={mutation.isPending} onClick={() => act(() => decisionTx(fleet.customerId, request.id, "cancel"))}>Cancel request</button>
          {mine && (values || enrollment) && <button disabled={mutation.isPending || expired || stale || !request.approved} onClick={() => act(() => executePolicyTx(client, fleet.customerId, request))}>Execute approved change</button>}
          {mine && !values && !enrollment && <p>Execute with this request ID through the exact secured operation adapter. This console supports policy-change execution.</p>}
        </div>}
      </article>;
    })}
    {error && <p role="alert">{error}</p>}
  </section>;
}
