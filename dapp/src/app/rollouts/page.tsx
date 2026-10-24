"use client";
import { useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useDeployments } from "@/hooks/useDeployments";
import { buildRolloutTx, readRollout, buildAdvanceRolloutTx, buildRolloutControlTx, type Rollout, type RolloutInput } from "@/lib/rollout";
import { PageHeader } from "@/components/ui";
import { bpsLabel, fetchRolloutDecisions, rolloutProgress, type RolloutProgress } from "@/lib/rollout-progress";
import { utc } from "@/lib/device-services";
import type { DeploymentEvent } from "@/lib/sui";
import type { Transaction } from "@mysten/sui/transactions";

const defaults: RolloutInput = { component: "", version: "", recipe: "", artifactSha256: "", recipeSha256: "",
  cohorts: [], concurrency: 1, rateCount: 1, rateWindowMs: 60000, bakeMs: 300000,
  minimumSamples: 1, failureBps: 1000, offlineTimeoutMs: 3600000 };
export default function RolloutsPage() {
  const client = useCurrentClient(); const account = useCurrentAccount();
  const { data: deployments = [] } = useDeployments();
  const [input, setInput] = useState(defaults); const [cohorts, setCohorts] = useState("");
  const [selected, setSelected] = useState(""); const [reason, setReason] = useState(""); const [skip, setSkip] = useState("");
  const [candidates, setCandidates] = useState("");
  const [error, setError] = useState(""); const [preparing, setPreparing] = useState(false);
  const { mutateAsync, isPending } = useFleetTransaction();
  const { data: rollout, refetch, error: readError } = useQuery({ queryKey: ["rollout", account?.address, selected],
    queryFn: () => readRollout(client, selected), enabled: !!selected, refetchInterval: 60_000 });
  const pending = isPending || preparing;
  async function submit(build: () => Transaction | Promise<Transaction>) {
    setError(""); setPreparing(true);
    try { await mutateAsync({ transaction: await build() }); if (selected) await refetch(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setPreparing(false); }
  }
  return <div className="max-w-4xl space-y-4">
    <PageHeader title="Staged rollouts" subtitle="Canary cohorts, bounded admission and audited health gates" />
    <p>Advance to admit the first cohort. After devices publish their signed post-apply health on chain, advance again to evaluate the gate. Coordinator state survives browser restarts. No background service submits coordinator transactions.</p>
    <p>Offline admitted devices retain capacity. Pausing or aborting stops new applies; an already admitted apply may finish. Each device supplies one health sample after its deployment outcome. Freshness is relative to its grant, independent of the daily health cadence.</p>
    {error && <p role="alert">{error}</p>}{readError && <p role="alert">{readError.message}</p>}
    <details className="border p-4"><summary>Create rollout from an authenticated recipe</summary>
      <form className="space-y-2" onSubmit={e => { e.preventDefault(); void submit(() => buildRolloutTx({ ...input,
        cohorts: cohorts.trim().split(/\n/).map(line => line.split(/[\s,]+/).filter(Boolean)) })); }}>
        <p>Use a recipe and its exact digests from the firmware upload. One cohort per line, with device IDs separated by spaces. Cohorts are an immutable snapshot of direct targets; adding group members never expands it.</p>
        {(["component", "version", "recipe", "artifactSha256", "recipeSha256"] as const).map(key => <label className="block" key={key}>{key}<input required className="border ml-2 p-1" value={input[key]} onChange={e => setInput({ ...input, [key]: e.target.value })} /></label>)}
        <label className="block">Cohorts<textarea required className="border block w-full" value={cohorts} onChange={e => setCohorts(e.target.value)} /></label>
        {([ ["concurrency", "Concurrent outstanding grants"], ["rateCount", "Grants per rolling window"], ["rateWindowMs", "Rate window (ms)"], ["bakeMs", "Bake period (ms)"], ["minimumSamples", "Minimum samples per cohort"], ["failureBps", "Abort failure threshold (basis points, 100 = 1%)"], ["offlineTimeoutMs", "Stall timeout before pause (ms)"] ] as const).map(([key,label]) => <label className="block" key={key}>{label}<input required type="number" min={key === "bakeMs" ? 0 : 1} className="border ml-2 p-1" value={input[key]} onChange={e => setInput({ ...input, [key]: Number(e.target.value) })} /></label>)}
        <button disabled={pending || !account}>Create without admitting devices</button>
      </form>
    </details>
    <RolloutOverview deployments={deployments.filter(d => d.schemaVersion === "3")} select={setSelected} />
    <label>Rollout <select className="border p-2" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select…</option>{deployments.filter(d => d.schemaVersion === "3").map(d => <option key={d.deploymentId} value={d.deploymentId}>{d.componentName} {d.version} · {d.deploymentId}</option>)}</select></label>
    {rollout && <div className="space-y-3 border p-4">
      <p role="status">{["Active", "Paused", "Aborted permanently", "Complete"][rollout.state]} · cohort {Number(rollout.cohort) + 1} · decision {rollout.revision}</p>
      <RolloutDetail id={selected} rollout={rollout} />
      <label className="block">Admission candidate IDs<input className="border ml-2" value={candidates} onChange={e => setCandidates(e.target.value)} /></label>
      <p>Choose reachable members to admit. Leave blank to evaluate health without new grants. Omitted offline members remain eligible for audited skips; admitted members retain capacity until they report.</p>
      <button disabled={pending || !account || rollout.state !== 0} onClick={() => void submit(() => buildAdvanceRolloutTx(client, selected, account!.address, candidates.split(/[\s,]+/).filter(Boolean)))}>Verify on-chain health and advance</button>
      <label className="block">Audit reason<input className="border ml-2" value={reason} onChange={e => setReason(e.target.value)} /></label>
      <label className="block">Skip never-admitted offline IDs (optional)<input className="border ml-2" value={skip} onChange={e => setSkip(e.target.value)} /></label>
      {[ [0,"Resume / apply skips"], [1,"Pause"], [2,"Abort permanently"] ].map(([state,label]) => <button className="mr-4" key={state} disabled={pending || !account || rollout.state >= 2} onClick={() => void submit(() => buildRolloutControlTx(selected, Number(state), skip.split(/[\s,]+/).filter(Boolean), reason))}>{label}</button>)}
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

function RolloutDetail({ id, rollout }: { id: string; rollout: Rollout }) {
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
    <details><summary>Members</summary><ul>{p.members.map(m => <li className="font-mono text-xs" key={m.device}>Cohort {m.cohort + 1} · {m.device} · {{ waiting: "Waiting", granted: "Granted; awaiting receipt", applied: "Applied", failed: "Failed", skipped: "Skipped" }[m.status]}{m.grantedAtMs !== null ? ` · granted ${utc(m.grantedAtMs)}` : ""}</li>)}</ul></details>
    <details><summary>Decision audit</summary>
      {error ? <p role="status">Decision history unavailable: {error.message}</p> : !history ? <p>Loading…</p> : <>
        <ul>{history.decisions.map(d => <li key={d.revision.toString()} className="text-xs">#{d.revision.toString()} · {utc(d.atMs)} · {d.state} · cohort {(d.cohort + BigInt(1)).toString()} · {d.reason}{d.admitted.length ? ` · granted ${d.admitted.length}` : ""}</li>)}</ul>
        {!history.complete && <p className="text-xs">Older decisions were not read.</p>}</>}
    </details>
  </div>;
}
