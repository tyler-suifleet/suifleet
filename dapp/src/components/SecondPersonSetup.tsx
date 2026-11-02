"use client";
import { useState } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useQueryClient } from "@tanstack/react-query";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { SECURITY_ACTIONS, buildSecurityPolicyTx, fetchOrganizationSecurity } from "@/lib/security-policy";
import { proposeEnrollmentTx, proposePolicyTx, secondPersonSupported } from "@/lib/second-person";

export function SecondPersonSetup() {
  const client = useCurrentClient(), { fleet } = useFleetSelection(), mutation = useFleetTransaction(), cache = useQueryClient();
  const [error, setError] = useState(""), [receipt, setReceipt] = useState("");
  const [requirements, setRequirements] = useState<number[]>(Array(7).fill(0));
  if (!fleet?.administrator || !secondPersonSupported()) return null;
  const customer = fleet.customerId;
  async function submit(work: () => Promise<Parameters<typeof mutation.mutateAsync>[0]["transaction"]> | Parameters<typeof mutation.mutateAsync>[0]["transaction"]) {
    try { setError(""); const result = await mutation.mutateAsync({ transaction: await work() }); setReceipt(result.digest); await cache.invalidateQueries({ queryKey: ["organization-security"] }); await cache.invalidateQueries({ queryKey: ["second-person-pending"] }); }
    catch (e) { setError(e instanceof Error ? e.message : "Security change failed"); }
  }
  return <section className="space-y-3" aria-label="Set up second-person approval"><h3>Set up second-person approval</h3>
    <p>First add at least two independently controlled administrator credentials. Any different current administrator may approve. The chain distinguishes addresses, not humans; do not use two keys controlled by one person. No approval service or support reset is involved. Each step costs a network fee.</p>
    <p>Create an enrollment request. Another administrator approves it on the dashboard; return as its proposer to execute it. This proves the independent credential works before the verifier can be enabled. Requests expire after one day. Creating the first request permanently upgrades this organization to the current approval protocol, even if enrollment is later cancelled. Update all organization clients first.</p>
    <button disabled={mutation.isPending} onClick={() => submit(() => proposeEnrollmentTx(client, customer, BigInt(Date.now() + 86400000)))}>Request verifier enrollment</button>
    <fieldset><legend>Choose actions requiring another administrator</legend>{SECURITY_ACTIONS.map((action, i) => <label key={action} className="block"><input type="checkbox" checked={requirements[i] === 4} onChange={e => setRequirements(values => values.map((v, index) => index === i ? e.target.checked ? 4 : 0 : v))} /> {action}</label>)}</fieldset>
    <p>Review the seven choices above; unchecked actions will be off. Saving an unprotected policy takes effect immediately. Changing a protected policy creates a one-day request for independent approval on the dashboard. Reducing protection requires the currently enforced factors; cancelling never weakens the policy. If every independent administrator credential is lost, administration can remain locked permanently.</p>
    <button disabled={mutation.isPending} onClick={() => submit(async () => { const policy = await fetchOrganizationSecurity(client, customer); if (!policy) throw Error("Migrate the organization first"); if (policy.requirements.some(v => v !== 0)) return proposePolicyTx(client, customer, requirements, BigInt(Date.now() + 86400000)); return buildSecurityPolicyTx(customer, requirements); })}>Save or request policy change</button>
    {receipt && <p className="break-all">Confirmed transaction: {receipt}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
