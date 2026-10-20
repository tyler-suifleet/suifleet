"use client";

import { useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildHandoverActionTx, buildHandoverStepsTx, buildProposeHandoverTx, readHandover, type HandoverItem } from "@/lib/fleet-handover";
import { PageHeader } from "@/components/ui";
import type { Transaction } from "@mysten/sui/transactions";

const KINDS = ["Device owner capability", "Device group", "Native deployment", "Revoke delegated role", "Close enrollment policy"];
const STATES = ["Awaiting recipient confirmation", "Confirmed; transfer in progress", "Cancelled", "Finished"];

export default function HandoverPage() {
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const mutation = useFleetTransaction();
  const [id, setId] = useState("");
  const [lookup, setLookup] = useState("");
  const [error, setError] = useState("");
  const [digest, setDigest] = useState("");
  const query = useQuery({ queryKey: ["handover", lookup], queryFn: () => readHandover(client, lookup), enabled: !!lookup });
  const h = query.data;
  const source = account?.address === h?.source;
  const recipient = account?.address === h?.recipient;
  function submit(build: () => Transaction) {
    setError("");
    try {
      mutation.mutate({ transaction: build() }, {
        onSuccess: result => { setDigest(result.digest); if (lookup) void query.refetch(); },
        onError: failure => setError(failure.message),
      });
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
  }
  const pending: HandoverItem[] = h ? h.objects.flatMap((objectId, index) => h.completed[index] ? [] : [{ objectId, kind: Number(h.kinds[index]) }]) : [];
  return <div className="max-w-4xl space-y-6">
    <PageHeader title="Transfer fleet administration" subtitle="Move authority to a replacement wallet or native Sui multisig without changing device keys." />
    <p>Both wallets must participate. Each batch commits completely or fails completely. Cancelling stops future batches; completed transfers remain with the recipient. If every authority key is lost, this flow cannot recover the fleet.</p>
    <p>Prepare the complete authority inventory first. Revoke old delegations, close old enrollment policies, and replace catalogs and catalog-bound deployments under the recipient. Rotate external credentials and review storage renewal ownership separately.</p>
    <form className="border rounded p-4 space-y-3" onSubmit={event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      submit(() => {
        const items: HandoverItem[] = JSON.parse(String(form.get("inventory")));
        if (!Array.isArray(items)) throw new Error("Inventory must be an array");
        return buildProposeHandoverTx(String(form.get("recipient")), items, BigInt(Date.now()) + BigInt(86400000));
      });
    }}>
      <h2 className="font-semibold">1. Source proposes a fixed inventory</h2>
      <label className="block">Recipient address<input required name="recipient" className="block border rounded p-2 w-full" placeholder="0x… wallet or multisig" /></label>
      <label className="block">Authority inventory<textarea required name="inventory" className="block border rounded p-2 w-full font-mono" rows={5} placeholder={'[{"objectId":"0x…","kind":0}]'} /></label>
      <p className="text-sm">Kinds: {KINDS.map((name, index) => `${index} ${name}`).join("; ")}. Proposals expire after 24 hours. Maximum 512 records; use separate handovers for larger fleets.</p>
      <button disabled={!account || mutation.isPending} className="border rounded px-3 py-2">Propose handover</button>
      {digest && <p className="break-all">Confirmed transaction: {digest}. Find the created Handover object in its effects, then share its ID with the recipient.</p>}
    </form>
    <form className="flex gap-2" onSubmit={event => { event.preventDefault(); setLookup(id.trim()); }}>
      <input required aria-label="Handover object ID" className="border rounded p-2 flex-1" value={id} onChange={event => setId(event.target.value)} placeholder="Handover object ID" />
      <button className="border rounded px-3">Load handover</button>
    </form>
    {(error || query.error) && <p role="alert" className="text-red-700">{error || query.error?.message}</p>}
    {h && <section className="border rounded p-4 space-y-3">
      <h2 className="font-semibold">2. Recipient verifies; source transfers; either party finishes</h2>
      <p>{STATES[h.state]} · Expires {new Date(Number(h.expires_ms)).toLocaleString()}</p>
      <p className="break-all">Source: {h.source}<br />Recipient: {h.recipient}</p>
      <table className="w-full text-sm"><thead><tr><th>Authority</th><th>Object</th><th>Progress</th></tr></thead><tbody>{h.objects.map((object, index) => <tr key={object}><td>{KINDS[Number(h.kinds[index])]}</td><td className="break-all">{object}</td><td>{h.completed[index] ? "Completed" : "Pending"}</td></tr>)}</tbody></table>
      <div className="flex flex-wrap gap-2">
        <button disabled={!recipient || h.state !== 0 || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverActionTx(lookup, "confirm"))}>Confirm as recipient</button>
        <button disabled={!source || h.state !== 1 || !pending.length || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverStepsTx(lookup, pending.slice(0, 16)))}>Transfer next {Math.min(pending.length, 16)} records</button>
        <button disabled={!(source || recipient) || h.state !== 1 || !!pending.length || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverActionTx(lookup, "finish"))}>Finish</button>
        <button disabled={!(source || recipient) || h.state > 1 || mutation.isPending} className="border rounded px-3 py-2" onClick={() => submit(() => buildHandoverActionTx(lookup, "cancel"))}>Cancel remaining work</button>
      </div>
    </section>}
  </div>;
}
