"use client";

import { useFleetSelection } from "@/hooks/useFleetSelection";
import { CustomerAccess } from "@/components/CustomerAccess";
import { useRef, useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildHandoverActionTx, buildHandoverStepsTx, buildProposeHandoverTx, readHandover, type HandoverItem } from "@/lib/fleet-handover";
import { PageHeader, InfoCard } from "@/components/ui";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { useDeployments } from "@/hooks/useDeployments";
import { normalizeSuiAddress } from "@mysten/sui/utils";
import { fleet_handover_ProposedBcs } from "@/lib/fleet-protocol";
import { MANIFEST, moveType } from "@/lib/deployment-manifest";
import type { Transaction } from "@mysten/sui/transactions";

const KINDS = ["Device administration", "Device group", "Update administration", "Remove delegated access", "Close enrollment policy"];
const STATES = ["Awaiting recipient confirmation", "Confirmed; transfer in progress", "Cancelled", "Finished"];

export default function HandoverPage() {
  const { supported } = useFleetSelection();
  return supported ? <CustomerAccess /> : <LegacyHandoverPage />;
}
function LegacyHandoverPage() {
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const mutation = useFleetTransaction();
  const transferSupported = !!MANIFEST?.type_origins["fleet_handover::Handover"];
  const devices = useDevices(), groups = useGroups(), deployments = useDeployments();
  const [chosen, setChosen] = useState<string[]>([]);
  const [additional, setAdditional] = useState("");
  const resources = [
    ...(devices.data ?? []).filter(d => d.ownerCapId).map(d => ({ objectId: d.ownerCapId!, kind: 0, label: d.name, category: "Device" })),
    ...(groups.data ?? []).filter(g => g.admin === account?.address).map(g => ({ objectId: g.objectId, kind: 1, label: g.name, category: "Group" })),
    ...(deployments.data ?? []).filter(d => d.creator === account?.address && ["1", "3"].includes(d.schemaVersion)).map(d => ({ objectId: d.deploymentId, kind: 2, label: `${d.componentName} ${d.version}`, category: "Update" })),
  ];
  const resourcesUnavailable = devices.isLoading || groups.isLoading || deployments.isLoading || !!devices.error || !!groups.error || !!deployments.error;
  const submitting = useRef(false);
  const [id, setId] = useState("");
  const [lookup, setLookup] = useState("");
  const [error, setError] = useState("");
  const [digest, setDigest] = useState("");
  const query = useQuery({ queryKey: ["handover", lookup], queryFn: () => readHandover(client, lookup), enabled: !!lookup });
  const h = query.data;
  const source = account?.address === h?.source;
  const recipient = account?.address === h?.recipient;
  function submit(build: () => Transaction, proposedRecipient?: string) {
    if (submitting.current) return;
    submitting.current = true;
    setError("");
    try {
      mutation.mutate({ transaction: build() }, {
        onSuccess: result => {
          setDigest(result.digest);
          if (proposedRecipient) {
            // Only confirmed events from the existing effects-checked transaction hook.
            // Bind the proposed reference to this request before offering it for sharing.
            const expected = [...resources.filter(r => chosen.includes(r.objectId)), ...(additional.trim() ? JSON.parse(additional) : [])];
            for (const event of result.events) {
              if (event.eventType !== moveType("fleet_handover::Handover").replace("::Handover", "::Proposed")) continue;
              try {
                const proposal = fleet_handover_ProposedBcs.parse(event.bcs);
                if (normalizeSuiAddress(proposal.source) !== normalizeSuiAddress(account!.address) || normalizeSuiAddress(proposal.recipient) !== normalizeSuiAddress(proposedRecipient)) continue;
                if (proposal.objects.length !== expected.length || proposal.objects.some((object, i) => normalizeSuiAddress(object) !== normalizeSuiAddress(expected[i].objectId) || Number(proposal.kinds[i]) !== expected[i].kind)) continue;
                setId(proposal.handover_id); setLookup(proposal.handover_id); return;
              } catch { /* Keep the confirmed receipt; never guess a transfer reference. */ }
            }
            setError("The request was recorded, but its transfer reference could not be verified. Do not create another request; ask your administrator to inspect the confirmation details.");
          } else if (lookup) void query.refetch();
        },
        onError: failure => setError(failure.message),
        onSettled: () => { submitting.current = false; },
      });
    } catch (failure) { submitting.current = false; setError(failure instanceof Error ? failure.message : String(failure)); }
  }
  const pending: HandoverItem[] = h ? h.objects.flatMap((objectId, index) => h.completed[index] ? [] : [{ objectId, kind: Number(h.kinds[index]) }]) : [];
  return <div className="max-w-4xl space-y-6">
    <PageHeader title="Administration" subtitle="Transfer control of selected devices and resources to another administrator." />
    <InfoCard title="Both administrators must approve the transfer">
      <p>You choose a specific list of resources. The new administrator reviews and accepts that same list before you can transfer anything. A request expires after 24 hours.</p>
      <p>Transferred resources stay with the new administrator. Cancelling stops only unfinished work. This cannot recover access if all administrator keys are lost.</p>
    </InfoCard>
    {!transferSupported && <p role="status" className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm">Administration transfers are not enabled in this fleet configuration. Ask your administrator to connect a supported configuration before starting a request.</p>}
    <ol className="grid gap-3 sm:grid-cols-3">{[['Choose', 'Select resources and the new administrator.'], ['Accept', 'The new administrator connects their account and accepts.'], ['Transfer', 'You approve the transfer in batches, then finish.']].map(([title, text], i) => <li key={title} className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"><span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-blue-50 text-xs font-semibold text-blue-700">{i + 1}</span><h2 className="mt-3 text-sm font-semibold">{title}</h2><p className="mt-1 text-xs leading-relaxed text-slate-500">{text}</p></li>)}</ol>
    <details className="rounded-xl border border-slate-200 bg-white p-5 text-sm"><summary>What needs a separate review?</summary><p className="mt-3 text-slate-600">This transfer does not move every kind of authority. Review delegated access, unused enrollment permissions, software publishing, storage renewal and external credentials separately. Catalog-based updates need replacement publishing under the new administrator. Moving device permissions does not rotate passwords or erase access to previously downloaded content.</p></details>
    <form className="rounded-xl border border-slate-200 bg-white p-6 space-y-4 shadow-sm" onSubmit={event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      submit(() => {
        const extra: HandoverItem[] = additional.trim() ? JSON.parse(additional) : [];
        if (!Array.isArray(extra)) throw new Error("Additional resources must be a list");
        const items = [...resources.filter(r => chosen.includes(r.objectId)).map(({objectId, kind}) => ({objectId, kind})), ...extra];
        return buildProposeHandoverTx(String(form.get("recipient")), items, BigInt(Date.now()) + BigInt(86400000));
      }, String(form.get("recipient")));
    }}>
      <h2 className="font-semibold">Choose what to transfer</h2>
      <label className="block text-sm">New administrator’s account address<input required name="recipient" className="mt-1 block border rounded p-2 w-full" placeholder="Paste the address shared by the new administrator" /></label>
      <p className="text-xs text-slate-500">Confirm this address directly with the recipient. Never ask for their private key or recovery phrase.</p>
      <fieldset disabled={resourcesUnavailable || mutation.isPending} className="space-y-2"><legend className="mb-2 text-sm font-medium">Resources you manage</legend>
        {resourcesUnavailable ? <p role="status" className="text-sm">Resources are loading or could not be verified. Refresh before selecting.</p> : resources.length === 0 ? <p className="text-sm text-slate-500">No supported resources found for this account. Older resources may be missing if history is incomplete.</p> : resources.map(resource => <label key={resource.objectId} className="flex items-start gap-3 rounded-lg border border-slate-200 p-3 text-sm"><input aria-label={`${resource.label} (${resource.category})`} className="mt-1" type="checkbox" checked={chosen.includes(resource.objectId)} onChange={event => setChosen(event.target.checked ? [...chosen, resource.objectId] : chosen.filter(id => id !== resource.objectId))} /><span><span className="font-medium">{resource.label}</span><span className="block text-xs text-slate-500">{resource.category}</span></span></label>)}
      </fieldset>
      <details className="text-sm"><summary>Advanced: specify additional resources</summary><p className="my-2 text-xs text-slate-500">For an administrator who has verified identifiers for resources not listed above. Duplicate or unsupported entries are rejected.</p><label>Additional resource list<textarea className="mt-1 block border rounded p-2 w-full font-mono" value={additional} onChange={e => setAdditional(e.target.value)} rows={3} placeholder={'[{"objectId":"0x…","kind":0}]'} /></label><p className="text-xs">Resource types: {KINDS.map((name, index) => `${index} ${name}`).join("; ")}. Maximum 512 resources per request.</p></details>
      <p className="text-sm text-slate-500">Creating this request does not transfer control. The recipient must accept first.</p>
      <button disabled={!transferSupported || !account || mutation.isPending || resourcesUnavailable || (!chosen.length && !additional.trim())} className="bg-blue-700 text-white rounded px-4 py-2 hover:bg-blue-800">Create transfer request</button>
      {digest && <details className="text-xs"><summary>Confirmation details</summary><p className="break-all">Confirmed transaction: {digest}</p></details>}
      {lookup && digest && <div className="rounded-lg bg-blue-50 p-3 text-sm"><p className="font-medium">Share this transfer reference with the new administrator</p><p className="mt-1 break-all font-mono text-xs">{lookup}</p><p className="mt-2 text-slate-600">They can connect their account and enter it below to review the request.</p></div>}
    </form>
    <form className="flex flex-wrap gap-2 rounded-xl border border-slate-200 bg-white p-5" onSubmit={event => { event.preventDefault(); setLookup(id.trim()); }}>
      <input required aria-label="Transfer reference" className="border rounded p-2 flex-1" value={id} onChange={event => setId(event.target.value)} placeholder="Transfer reference" />
      <button disabled={!transferSupported} className="border rounded px-3">Review transfer</button>
    </form>
    {(error || query.error) && <p role="alert" className="text-red-700">{error || query.error?.message}</p>}
    {h && <section className="rounded-xl border border-slate-200 bg-white p-6 space-y-4 shadow-sm">
      <h2 className="font-semibold">Review and complete the transfer</h2>
      <p>{STATES[h.state]} · Expires {new Date(Number(h.expires_ms)).toLocaleString()}</p>
      <p className="break-all">Current administrator: {h.source}<br />New administrator: {h.recipient}</p>
      <table className="w-full text-sm"><thead><tr><th>Resource type</th><th>Resource</th><th>Progress</th></tr></thead><tbody>{h.objects.map((object, index) => <tr key={object}><td>{KINDS[Number(h.kinds[index])]}</td><td><details><summary>{resources.find(resource => normalizeSuiAddress(resource.objectId) === normalizeSuiAddress(object))?.label ?? `Reference ${object.slice(0, 10)}…`}</summary><p className="break-all text-xs">{object}</p></details></td><td>{h.completed[index] ? "Completed" : "Pending"}</td></tr>)}</tbody></table>
      <div className="flex flex-wrap gap-2">
        <button disabled={!recipient || h.state !== 0 || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverActionTx(lookup, "confirm"))}>Accept as new administrator</button>
        <button disabled={!source || h.state !== 1 || !pending.length || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverStepsTx(lookup, pending.slice(0, 16)))}>Transfer next {Math.min(pending.length, 16)} resources</button>
        <button disabled={!(source || recipient) || h.state !== 1 || !!pending.length || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverActionTx(lookup, "finish"))}>Finish transfer</button>
        <button disabled={!(source || recipient) || h.state > 1 || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverActionTx(lookup, "cancel"))}>Cancel remaining work</button>
      </div>
    </section>}
  </div>;
}
