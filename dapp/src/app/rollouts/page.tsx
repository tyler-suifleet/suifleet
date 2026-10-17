"use client";
import { useState } from "react";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useDeployments } from "@/hooks/useDeployments";
import { useTelemetryToken } from "@/hooks/useFleetHealth";
import { buildRolloutTx, readRollout, buildAdvanceRolloutTx, buildRolloutControlTx, type RolloutInput } from "@/lib/rollout";
import { PageHeader } from "@/components/ui";
import type { Transaction } from "@mysten/sui/transactions";

const defaults: RolloutInput = { component: "", version: "", recipe: "", artifactSha256: "", recipeSha256: "",
  cohorts: [], concurrency: 1, rateCount: 1, rateWindowMs: 60000, bakeMs: 300000,
  minimumSamples: 1, failureBps: 1000, offlineTimeoutMs: 600000 };
export default function RolloutsPage() {
  const client = useCurrentClient(); const account = useCurrentAccount(); const token = useTelemetryToken();
  const { data: deployments = [] } = useDeployments();
  const [input, setInput] = useState(defaults); const [cohorts, setCohorts] = useState("");
  const [selected, setSelected] = useState(""); const [reason, setReason] = useState(""); const [skip, setSkip] = useState("");
  const [error, setError] = useState(""); const [preparing, setPreparing] = useState(false);
  const { mutateAsync, isPending } = useFleetTransaction();
  const { data: rollout, refetch, error: readError } = useQuery({ queryKey: ["rollout", selected],
    queryFn: () => readRollout(client, selected), enabled: !!selected, refetchInterval: 10000 });
  const pending = isPending || preparing;
  async function submit(build: () => Transaction | Promise<Transaction>) {
    setError(""); setPreparing(true);
    try { await mutateAsync({ transaction: await build() }); await refetch(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setPreparing(false); }
  }
  return <div className="max-w-4xl space-y-4">
    <PageHeader title="Staged rollouts" subtitle="Canary cohorts, bounded admission and audited health gates" />
    <p>Keep this page available and advance after fresh telemetry arrives. Coordinator state lives on chain and survives browser restarts. No background service submits transactions. Set the telemetry read token on Devices first.</p>
    <p>Offline admitted devices retain capacity. Pausing or aborting stops new applies; an already admitted apply may finish. Health is sampled when you advance, with a fresh signed report of this deployment installed.</p>
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
    <label>Rollout <select className="border p-2" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select…</option>{deployments.filter(d => d.schemaVersion === "3").map(d => <option key={d.deploymentId} value={d.deploymentId}>{d.componentName} {d.version} · {d.deploymentId}</option>)}</select></label>
    {rollout && <div className="space-y-3 border p-4">
      <p role="status">{["Active", "Paused", "Aborted permanently", "Complete"][rollout.state]} · cohort {Number(rollout.cohort) + 1} · decision {rollout.revision}</p>
      <p>Concurrency {rollout.policy.concurrency}; rate {rollout.policy.rate_count}/{rollout.policy.rate_window_ms}ms; bake {rollout.policy.bake_ms}ms; minimum sample {rollout.policy.minimum_samples}; abort at {Number(rollout.policy.failure_bps) / 100}% failures.</p>
      <ul>{rollout.members.map((id,i) => <li className="font-mono text-xs" key={id}>Cohort {Number(rollout.cohorts[i])+1} · {id} · {rollout.skipped[i] ? "Skipped" : rollout.outcomes[i] ? ["", "Applied", "Failed"][rollout.outcomes[i]] : BigInt(rollout.admitted_at[i]) > BigInt(0) ? "Admitted; awaiting receipt" : "Waiting"}</li>)}</ul>
      <button disabled={pending || !account || rollout.state !== 0} onClick={() => void submit(() => buildAdvanceRolloutTx(client, selected, account!.address, token))}>Verify current health and advance</button>
      <label className="block">Audit reason<input className="border ml-2" value={reason} onChange={e => setReason(e.target.value)} /></label>
      <label className="block">Skip never-admitted offline IDs (optional)<input className="border ml-2" value={skip} onChange={e => setSkip(e.target.value)} /></label>
      {[ [0,"Resume / apply skips"], [1,"Pause"], [2,"Abort permanently"] ].map(([state,label]) => <button className="mr-4" key={state} disabled={pending || !account || rollout.state >= 2} onClick={() => void submit(() => buildRolloutControlTx(selected, Number(state), skip.split(/[\s,]+/).filter(Boolean), reason))}>{label}</button>)}
    </div>}
  </div>;
}
