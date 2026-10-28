"use client";

import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetDataConfig } from "@/hooks/useFleetDataConfig";
import { useEffect, useRef, useState } from "react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useDeployments } from "@/hooks/useDeployments";
import { buildRolloutTx, rolloutAnnouncements, readRollout, buildAdvanceRolloutTx, buildRolloutControlTx, type Rollout } from "@/lib/rollout";
import { RolloutBuilder } from "@/components/RolloutBuilder";
import { useDevices } from "@/hooks/useDevices";
import { decisionMessage } from "@/lib/rollout-form";
import { deployment_manager_DeploymentCreatedBcs, deployment_manager_RolloutDecisionBcs } from "@/lib/fleet-protocol";
import { moveType } from "@/lib/deployment-manifest";
import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { useQueryClient } from "@tanstack/react-query";
import { PageHeader } from "@/components/ui";
import { bpsLabel, fetchRolloutDecisions, rolloutProgress, type RolloutProgress } from "@/lib/rollout-progress";
import { utc } from "@/lib/device-services";
import { fetchFleetDevices } from "@/lib/sui";
import type { DeploymentEvent } from "@/lib/sui";
import type { Transaction } from "@mysten/sui/transactions";

export default function RolloutsPage() {
  const { fleet } = useFleetSelection();
  const dataConfig = useFleetDataConfig();
  const client = useCurrentClient(); const account = useCurrentAccount();
  const { data: deployments = [] } = useDeployments();
  const devices = useDevices(); const cache = useQueryClient(); const submitting = useRef(false);
  const [message,setMessage] = useState(""); const [created,setCreated] = useState("");

  const [selected, setSelected] = useState(""); const [reason, setReason] = useState(""); const [skip, setSkip] = useState("");
  const [candidates, setCandidates] = useState("");
  const [error, setError] = useState(""); const [preparing, setPreparing] = useState(false);
  const { mutateAsync, isPending } = useFleetTransaction();
  const { data: rollout, refetch, error: readError } = useQuery({ queryKey: ["rollout", account?.address, selected],
    queryFn: () => readRollout(client, selected), enabled: !!selected, refetchInterval: 60_000 });
  const missingMembers = rollout?.members.filter(id=>!devices.data?.some(d=>normalizeSuiAddress(d.objectId)===normalizeSuiAddress(id))) ?? [];
  const memberDevices = useQuery({queryKey:["rollout-member-names",account?.address,...missingMembers],enabled:missingMembers.length>0,queryFn:()=>fetchFleetDevices(client,missingMembers)});
  const names = new Map([...(devices.data ?? []),...(memberDevices.data ?? [])].map(d=>[normalizeSuiAddress(d.objectId),d.name]));
  useEffect(()=>{setSelected("");setCandidates("");setMessage("");setCreated("");setError("");},[account?.address,fleet?.id]);
  const pending = isPending || preparing;
  async function submit(build: () => Transaction | Promise<Transaction>, creating = false) {
    if (submitting.current) throw Error("A transaction is already pending");
    submitting.current = true;
    setError(""); setPreparing(true);
    let confirmed = "";
    try {
      setMessage(""); const result = await mutateAsync({ transaction: await build() });
      confirmed = result.digest;
      const decisions = result.events.filter(e=>normalizeStructTag(e.eventType)===normalizeStructTag(moveType("deployment_manager::RolloutDecision"))).map(e=>deployment_manager_RolloutDecisionBcs.parse(e.bcs));
      const d = decisions.find(d=>creating || normalizeSuiAddress(d.deployment_id)===normalizeSuiAddress(selected));
      setMessage(`Confirmed ${result.digest}. ` + (d ? decisionMessage({ revision:BigInt(d.revision), cohort:BigInt(d.cohort), state:(["Active","Paused","Aborted","Complete"] as const)[d.state], reason:d.reason, atMs:BigInt(d.at_ms), admitted:d.admitted }, candidates.split(/[\s,]+/).filter(Boolean).length) : "Decision details unavailable; inspect the refreshed panel before retrying."));
      if (creating) {
        const event = result.events.find(e=>normalizeStructTag(e.eventType)===normalizeStructTag(moveType("deployment_manager::DeploymentCreated")));
        if (event || d) { const id=event ? deployment_manager_DeploymentCreatedBcs.parse(event.bcs).deployment_id : d!.deployment_id; setSelected(id);setCreated(id); }
      }
      setCandidates("");
      await cache.invalidateQueries({queryKey:["deployments"]});
      await cache.invalidateQueries({queryKey:["rollout-overview"]});
      if (selected && !creating) await refetch();
    }
    catch (e) {
      if (confirmed) setMessage(`Confirmed ${confirmed}. Display refresh unavailable: ${e instanceof Error ? e.message : String(e)}. Inspect this transaction before creating another rollout.`);
      else { setError(e instanceof Error ? e.message : String(e)); if(creating) throw e; }
    }
    finally { setPreparing(false); submitting.current = false; }
  }
  return <div className="max-w-4xl space-y-4">
    <PageHeader title="Staged rollouts" subtitle="Send updates to a small group first, then expand once device health checks pass" />
    <section className="rounded-xl border border-slate-200 bg-white p-5 space-y-3 text-sm text-slate-600"><h2 className="text-slate-900">How staged rollouts work</h2><p>Advance to admit the first cohort. After devices report verified health following an update, advance again to evaluate the gate. Progress is saved if you close the browser. Return here to approve each next step.</p>
    <p>Offline admitted devices retain capacity. Pausing or aborting stops new applies; an already admitted apply may finish. Each device supplies one health sample after its deployment outcome. Freshness is relative to its grant, independent of the daily health cadence.</p></section>
    {!account && <p>Connect your wallet to create or control rollouts. You can still view available rollout records.</p>}
    {error && <p role="alert">{error}</p>}{readError && <p role="alert">{readError.message}</p>}
    {message && <p role="status" className="break-all">{message} {created && <a className="underline" href="#rollout-panel" onClick={()=>{setSelected(created);setCandidates("");}}>Open created rollout panel</a>}</p>}
    <RolloutBuilder key={`${account?.address}/${fleet?.id ?? ''}`} busy={pending} create={input=>submit(async()=>buildRolloutTx(input, fleet ? undefined : await rolloutAnnouncements(client,account!.address,input.cohorts.flat()),fleet),true)} />
    <RolloutOverview deployments={deployments.filter(d => d.schemaVersion === "3")} select={id=>{setSelected(id);setCandidates("");}} />
    <label>Rollout <select className="border p-2" disabled={pending} value={selected} onChange={e => {setSelected(e.target.value);setCandidates("");}}><option value="">Select…</option>{deployments.filter(d => d.schemaVersion === "3").map(d => <option key={d.deploymentId} value={d.deploymentId}>{d.componentName} {d.version} · {d.deploymentId}</option>)}</select></label>
    {memberDevices.error && <p role="status">Some member names are unavailable: {memberDevices.error.message}</p>}
    {rollout && <div id="rollout-panel" className="space-y-3 border p-4">
      <p role="status">{["Active", "Paused", "Aborted permanently", "Complete"][rollout.state]} · cohort {Number(rollout.cohort) + 1} · decision {rollout.revision}</p>
      <RolloutDetail id={selected} rollout={rollout} names={names} />
      <fieldset disabled={pending}><legend>Select waiting devices to admit in the current cohort</legend>{rollout.members.filter((_,i)=>rollout.cohorts[i]===rollout.cohort && rollout.admitted_at[i]==="0" && !rollout.skipped[i]).map(id=><label className="block" key={id}><input type="checkbox" checked={candidates.split(' ').includes(id)} onChange={e=>setCandidates(e.target.checked ? [...candidates.split(' ').filter(Boolean),id].join(' ') : candidates.split(' ').filter(v=>v!==id).join(' '))}/>{names.get(normalizeSuiAddress(id)) ?? 'Unavailable device'}<details><summary>Object ID</summary>{id}</details></label>)}</fieldset>
      <p>Choose reachable members to admit. Leave blank to evaluate health without new grants. Omitted offline members remain eligible for audited skips; admitted members retain capacity until they report.</p>
      <button disabled={pending || !account || rollout.state !== 0} onClick={() => void submit(() => buildAdvanceRolloutTx(client, selected, account!.address, candidates.split(/[\s,]+/).filter(Boolean), fleet ? { fleet, config: dataConfig } : undefined))}>Check device health and continue</button>
      <label className="block">Audit reason<input className="border ml-2" value={reason} onChange={e => setReason(e.target.value)} /></label>
      <label className="block">Skip never-admitted offline IDs (optional)<input className="border ml-2" value={skip} onChange={e => setSkip(e.target.value)} /></label>
      {[ [0,"Resume / apply skips"], [1,"Pause"], [2,"Abort permanently"] ].map(([state,label]) => <button className="mr-4" key={state} disabled={pending || !account || rollout.state >= 2} onClick={() => void submit(() => buildRolloutControlTx(selected, Number(state), skip.split(/[\s,]+/).filter(Boolean), reason, fleet))}>{label}</button>)}
    </div>}
  </div>;
}

/** Progress or the reason it cannot be shown; an inconsistent record is never partially displayed. */
function progressOf(rollout: Rollout): RolloutProgress | string {
  try { return rolloutProgress(rollout, Date.now()); } catch (e) { return e instanceof Error ? e.message : String(e); }
}

const STAGE = { done: "Done", current: "Current", pending: "Pending" } as const;

function RolloutOverview({ deployments, select }: { deployments: DeploymentEvent[]; select: (id: string) => void }) {
  const client = useCurrentClient();
  const ids = deployments.map(d => d.deploymentId);
  const { data, error } = useQuery({ queryKey: ["rollout-overview", ...ids], enabled: ids.length > 0, refetchInterval: 60_000,
    queryFn: () => Promise.all(ids.map(async id => { try { return progressOf(await readRollout(client, id)); }
      catch (e) { return `Rollout record unavailable: ${e instanceof Error ? e.message : String(e)}`; } })) });
  if (!ids.length) return <p>No staged rollouts created by this account.</p>;
  if (error) return <p role="alert">Rollout records unavailable: {error.message}</p>;
  return <table className="text-sm w-full text-left" data-testid="rollout-overview"><thead><tr><th>Rollout</th><th>State</th><th>Stage</th><th>Granted</th><th>Applied / failed</th><th>Failure threshold</th></tr></thead>
    <tbody>{deployments.map((d, i) => { const p = data?.[i]; return <tr key={d.deploymentId} className="border-t">
      <td><button className="underline text-blue-700" onClick={() => select(d.deploymentId)}>{d.componentName} {d.version}</button></td>
      {p === undefined ? <td colSpan={5}>Loading…</td> : typeof p === "string" ? <td colSpan={5} role="status">Unavailable: {p}</td> : <>
        <td className={p.aborted ? "text-red-700 font-medium" : ""}>{p.state}</td>
        <td>{p.currentCohort + 1} of {p.stages.length}</td>
        <td>{p.stages.reduce((n, s) => n + s.granted, 0)} of {p.members.length}</td>
        <td>{p.samples - p.failures} / {p.failures}</td>
        <td>{bpsLabel(p.policy.failureBps)} after {p.policy.minimumSamples.toString()} samples{p.thresholdReached ? " · reached" : ""}</td></>}
    </tr>; })}</tbody></table>;
}

function RolloutDetail({ id, rollout, names }: { id: string; rollout: Rollout; names: Map<string,string> }) {
  const client = useCurrentClient();
  const p = progressOf(rollout);
  const revision = typeof p === "string" ? undefined : p.revision;
  const { data: history, error } = useQuery({ queryKey: ["rollout-decisions", id, revision?.toString()], enabled: revision !== undefined,
    queryFn: () => fetchRolloutDecisions(client, id, revision!) });
  if (typeof p === "string") return <p role="alert">Rollout record cannot be shown: {p}</p>;
  const latest = history?.decisions[0];
  return <div className="space-y-2" data-testid="rollout-detail">
    {p.aborted && <p role="alert" className="text-red-700">Aborted permanently{latest && latest.state === "Aborted" ? `: ${latest.reason}` : ""}. No new grants can be issued.</p>}
    <p>Concurrency {p.policy.concurrency.toString()}; rate {p.policy.rateCount.toString()}/{p.policy.rateWindowMs.toString()}ms; bake {p.policy.bakeMs.toString()}ms; minimum sample {p.policy.minimumSamples.toString()}; abort at {bpsLabel(p.policy.failureBps)} failures; stall pause after {p.policy.offlineTimeoutMs.toString()}ms.</p>
    <p>Outcomes: {p.samples} settled, {p.failures} failed{p.samples ? ` (${bpsLabel(BigInt(p.failures) * BigInt(10000) / BigInt(p.samples))})` : ""}{p.thresholdReached ? "; failure threshold reached" : ""}.{p.bake ? ` Baking cohort ${p.currentCohort + 1}: ${p.bake.remainingMs.toString()}ms remaining.` : ""}</p>
    <table className="text-sm text-left" data-testid="rollout-stages"><thead><tr><th className="pr-3">Cohort</th><th className="pr-3">Stage</th><th className="pr-3">Members</th><th className="pr-3">Granted</th><th className="pr-3">In flight</th><th className="pr-3">Applied</th><th className="pr-3">Failed</th><th className="pr-3">Skipped</th><th>Waiting</th></tr></thead>
      <tbody>{p.stages.map(s => <tr key={s.cohort}><td>{s.cohort + 1}</td><td>{STAGE[s.status]}</td><td>{s.members}</td><td>{s.granted}</td><td>{s.inflight}</td><td>{s.applied}</td><td>{s.failed}</td><td>{s.skipped}</td><td>{s.waiting}</td></tr>)}</tbody></table>
    <details><summary>Members</summary><ul>{p.members.map(m => <li className="font-mono text-xs" key={m.device}>Cohort {m.cohort + 1} · {names.get(m.device) ?? "Unavailable device"}<details><summary>Object ID</summary>{m.device}</details> · {{ waiting: "Waiting", granted: "Granted; awaiting receipt", applied: "Applied", failed: "Failed", skipped: "Skipped" }[m.status]}{m.grantedAtMs !== null ? ` · granted ${utc(m.grantedAtMs)}` : ""}</li>)}</ul></details>
    <details><summary>Decision audit</summary>
      {error ? <p role="status">Decision history unavailable: {error.message}</p> : !history ? <p>Loading…</p> : <>
        <ul>{history.decisions.map(d => <li key={d.revision.toString()} className="text-xs">#{d.revision.toString()} · {utc(d.atMs)} · {d.state} · cohort {(d.cohort + BigInt(1)).toString()} · {decisionMessage(d)}{d.admitted.map(id=><span key={id}> · {names.get(id) ?? "Unavailable device"}<details><summary>Object ID</summary>{id}</details></span>)}</li>)}</ul>
        {!history.complete && <p className="text-xs">Older decisions were not read.</p>}</>}
    </details>
  </div>;
}
