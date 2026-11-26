"use client";

import { useFleetSelection } from "@/hooks/useFleetSelection";

import { useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { PageHeader, ConnectionRequired } from "@/components/ui";
import { DeviceClientHelp, GuideLink } from "@/components/DeviceClientHelp";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { useFleetTransaction, type TransactionPhase } from "@/hooks/useFleetTransaction";
import { NETWORK } from "@/lib/constants";
import { MANIFEST, requireManifest } from "@/lib/deployment-manifest";
import {
  BROWSER_CREDENTIAL_LIMIT, LIMITS, buildClosePolicyTx, buildCreatePolicyTx, buildRevokeClaimsTx, buildSetLimitsTx, buildTransferTx,
  checkReservations, chunk, createdPolicy, policyFromEvents, credentialArchive, entryStatuses, fetchPolicies, generateCredentials, loadJob, newJob,
  observeRegistrations, recoverInterruptedRegistrations, parseJob, parseRoster, plan, policyAccepting, saveJob, snapshot, storedReservations,
  type EnrollmentJob, type EntryStatus, type EntryView, type PolicyState, type ReservationCheck,
} from "@/lib/enrollment";
import type { DeviceGroup, DeviceObject } from "@/lib/sui";
import type { Transaction } from "@mysten/sui/transactions";
import { TransactionFeedback, useTransactionFeedback } from "@/components/TransactionFeedback";
import { transactionStage } from "@/lib/tx-feedback";
import { parseSerials, planLostCredentials, type LostCredentialPlan } from "@/lib/credential-recovery";
import Link from "next/link";

const STATUS_LABEL: Record<EntryStatus, string> = {
  unregistered: "Reserved", unfunded: "Needs enrollment gas", "awaiting-device": "Funded, unused", revoked: "Revoked",
  confirming: "Confirming registration", withdrawn: "Claim withdrawn", binding: "Consumed, pending binding", complete: "Bound", conflict: "Conflict", untrusted: "Not yours",
};
const MIST = BigInt(1_000_000_000);
const sui = (mist: bigint) => `${Number(mist) / 1e9} SUI`;
/** A positive SUI amount typed by the operator, in MIST; undefined if invalid. */
const toMist = (value: string) => { const n = Number(value); return Number.isFinite(n) && n > 0 ? BigInt(Math.round(n * 1e9)) : undefined; };
const input = "border border-gray-300 rounded px-2 py-1.5 text-sm";
const button = "border border-gray-300 rounded px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-40";
const primary = "bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white rounded px-3 py-1.5 text-sm disabled:opacity-40";

function download(name: string, body: Uint8Array | string, type: string) {
  const url = URL.createObjectURL(new Blob([body as BlobPart], { type }));
  const link = document.createElement("a");
  link.href = url; link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function EnrollmentPage() {
  const selection = useFleetSelection(), fleet = selection.fleet;
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const { mutateAsync } = useFleetTransaction();
  const [selected, setSelected] = useState<string>("");
  // Policies this page created: read directly, before their creation events are indexed.
  const [created, setCreated] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const failure = useTransactionFeedback();
  const policies = useQuery({ queryKey: ["enrollment-policies", NETWORK, account?.address, selection.identity, created], queryFn: () => fetchPolicies(client, fleet?.id ?? account!.address, created, fleet),
    enabled: !!account?.address, refetchInterval: 60_000, placeholderData: keepPreviousData });
  const policy = policies.data?.find((p) => p.id === selected);
  const loadingSelected = !!selected && !policy && policies.isFetching;
  // Start with a batch that can accept enrollments, so a returning operator has nothing to choose.
  useEffect(() => {
    if (selected || !policies.data?.length) return;
    const open = policies.data.find((p) => !policyAccepting(p));
    if (open) setSelected(open.id);
  }, [selected, policies.data]);

  async function run(transaction: Transaction) {
    setBusy(true); setError(""); failure.clear();
    try { return await mutateAsync({ transaction }); }
    catch (e) { failure.report(e, "The batch change"); return undefined; }
    finally { setBusy(false); }
  }

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const days = Number(fd.get("days"));
    const result = await run(buildCreatePolicyTx(BigInt(fd.get("quota") as string), days > 0 ? BigInt(Date.now() + days * 86_400_000) : BigInt(0), fleet));
    if (!result) return;
    let id;
    try { id = policyFromEvents(result.events) ?? await createdPolicy(client, result.digest); }
    catch (e) { setError(`The batch was created, but could not be read yet: ${(e as Error).message}. Refresh the page instead of creating another.`); return; }
    setCreated((c) => c.includes(id) ? c : [...c, id]);
    setSelected(id);
  }

  const hasPolicies = !!policies.data?.length || loadingSelected;
  const createForm = (
    <form onSubmit={create} className="flex flex-wrap gap-2 items-end">
      <label>Quota <input name="quota" type="number" min={1} defaultValue={100} required className={`${input} w-28 ml-1`} /></label>
      <label>Valid for days (0 = no expiry) <input name="days" type="number" min={0} defaultValue={14} required className={`${input} w-20 ml-1`} /></label>
      <button type="submit" disabled={busy || loadingSelected} className={primary}>Create batch</button>
    </form>
  );

  if (!account) return <ConnectionRequired title="Enrollment" description="Prepare device batches and securely enroll devices with single-use credentials." />;
  return (
    <div className="space-y-4 max-w-5xl text-sm">
      <PageHeader title="Enrollment" subtitle="Add devices by serial number. Each device enrolls itself with a one-time credential file; no addresses to copy." />
      <details className="rounded border border-gray-200 bg-white px-4 py-2">
        <summary className="cursor-pointer font-medium">How device enrollment works</summary>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-gray-700">
          <li>Choose or create an enrollment batch.</li>
          <li>Add devices by serial number and download their credential file.</li>
          <li>Register the credentials with one wallet approval.</li>
          <li>Give each device its own folder from the file; it enrolls itself and appears under Devices.</li>
        </ol>
        <div className="mt-3"><DeviceClientHelp variant="enroll" /></div>
      </details>
      {fleet && <CapacityNotice active={fleet.activeDevices} limit={fleet.deviceLimit} />}
      {error && <p role="alert" className="text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{error}</p>}
      <TransactionFeedback feedback={failure.feedback} onDismiss={failure.clear} />
      <section className="bg-white border border-gray-200 rounded p-4 space-y-3">
        <h2 className="font-semibold">1. Enrollment batch</h2>
        {hasPolicies ? <div className="flex flex-wrap gap-2 items-center">
          <label htmlFor="policy">Batch</label>
          <select id="policy" className={input} value={selected} onChange={(e) => setSelected(e.target.value)}>
            <option value="">Select a batch…</option>
            {policies.data?.map((p) => <option key={p.id} value={p.id}>{policyLabel(p)}</option>)}
            {loadingSelected && <option value={selected}>New batch · loading…</option>}
          </select>
          {loadingSelected && <span role="status" className="text-gray-500">Loading the new batch…</span>}
        </div> : <p className="text-gray-500">{policies.isLoading ? "Loading your batches…" : "A batch sets how many devices may enroll and until when. Create one to start; the defaults suit most fleets."}</p>}
        {hasPolicies ? <details>
          <summary className="cursor-pointer text-[#2e6fb0]">Create another batch</summary>
          <div className="mt-2">{createForm}</div>
        </details> : !policies.isLoading && createForm}
        {policy && <PolicyControls policy={policy} busy={busy} run={async (tx) => { if (await run(tx)) await policies.refetch(); }} />}
      </section>
      {policy && <JobPanel key={policy.id} policy={policy} operator={fleet?.id ?? account.address} freeSlots={fleet ? (fleet.deviceLimit > fleet.activeDevices ? Number(fleet.deviceLimit - fleet.activeDevices) : 0) : undefined} onError={setError} onFailure={(e, action) => { setError(""); failure.report(e, action); }} />}
    </div>
  );
}

function policyLabel(p: PolicyState) {
  const state = policyAccepting(p);
  return `Batch ${p.id.slice(2, 8)} · ${p.enrolled} enrolled · ${p.remaining} left${p.unusedClaims ? ` · ${p.unusedClaims} unused claims` : ""}${state ? ` · ${state}` : ""}`;
}

function PolicyControls({ policy, busy, run }: { policy: PolicyState; busy: boolean; run: (tx: Transaction) => Promise<void> }) {
  const [quota, setQuota] = useState(policy.remaining.toString());
  const [days, setDays] = useState("14");
  const state = policyAccepting(policy);
  return (
    <div className="border-t pt-3 space-y-2" data-testid="policy-state">
      <p>
        {state ? <span className="text-amber-700 font-medium">Not accepting enrollments: {state}. </span> : <span className="text-green-700 font-medium">Accepting enrollments. </span>}
        {policy.enrolled.toString()} enrolled · {policy.remaining.toString()} remaining · {policy.unusedClaims} unused claims ·{" "}
        {policy.expiresAtMs === BigInt(0) ? "no expiry" : `expires ${new Date(Number(policy.expiresAtMs)).toLocaleString()}`}
      </p>
      {policy.open && <details><summary className="cursor-pointer text-[#2e6fb0]">Change limits or close this batch</summary><div className="mt-2 flex flex-wrap gap-2 items-end">
        <label>Remaining quota <input value={quota} onChange={(e) => setQuota(e.target.value)} type="number" min={0} className={`${input} w-28 ml-1`} /></label>
        <label>Valid for days from now (0 = no expiry) <input value={days} onChange={(e) => setDays(e.target.value)} type="number" min={0} className={`${input} w-20 ml-1`} /></label>
        <button disabled={busy} className={button} onClick={() => run(buildSetLimitsTx(policy.id, BigInt(quota || 0), Number(days) > 0 ? BigInt(Date.now() + Number(days) * 86_400_000) : BigInt(0), policy.fleet))}>Set limits</button>
        <button disabled={busy} className={`${button} text-red-600`} onClick={() => confirm("Close this batch permanently? No device can enroll under it afterwards.") && run(buildClosePolicyTx(policy.id, policy.fleet))}>Close batch</button>
      </div></details>}
    </div>
  );
}

type AddMode = "csv" | "pair" | "roster";

function JobPanel({ policy, operator, freeSlots, onError, onFailure }: { policy: PolicyState; operator: string; freeSlots?: number; onError: (message: string) => void; onFailure: (error: unknown, action: string) => void }) {
  const client = useCurrentClient();
  const devicesQuery = useDevices(), groupsQuery = useGroups();
  const devices = useMemo(() => devicesQuery.data ?? [], [devicesQuery.data]), groups = useMemo(() => groupsQuery.data ?? [], [groupsQuery.data]);
  // Checking rows against a partly loaded inventory would reject real groups and miss duplicates.
  const inventoryError = devicesQuery.error ?? groupsQuery.error;
  const inventory: Inventory = inventoryError ? { ready: false, error: `Your devices and groups could not be loaded: ${(inventoryError as Error).message}`, retry: () => { void devicesQuery.refetch(); void groupsQuery.refetch(); } }
    : { ready: devicesQuery.isSuccess && groupsQuery.isSuccess };
  const { mutateAsync } = useFleetTransaction();
  const [job, setJobState] = useState<EnrollmentJob>(() => {
    try { return loadJob(localStorage, operator, policy.id) ?? newJob(operator, policy.id); }
    catch (e) { onError(`Stored job unreadable: ${(e as Error).message}`); return newJob(operator, policy.id); }
  });
  const setJob = (next: EnrollmentJob) => { saveJob(localStorage, next); setJobState(next); };
  const jobRef = useRef(job); jobRef.current = job;
  const [progress, setProgress] = useState<{ step: number; total: number; label: string; phase?: TransactionPhase } | null>(null);
  const stop = useRef(false);
  // A second click can arrive before the progress state renders (for example
  // after a confirmation dialog); never start a second run of wallet requests.
  const running = useRef(false);
  const chain = useQuery({
    queryKey: ["enrollment-job", NETWORK, policy.id, job.entries.length],
    queryFn: () => snapshot(client, jobRef.current, policy.fleet),
    refetchInterval: 60_000,
  });
  useEffect(() => {
    if (!chain.data) return;
    const next = observeRegistrations(jobRef.current, chain.data);
    if (next !== jobRef.current) { jobRef.current = next; saveJob(localStorage, next); setJobState(next); }
  }, [chain.data]);
  const views = useMemo(() => chain.data ? entryStatuses(job, chain.data) : [], [job, chain.data]);
  const steps = chain.data ? plan(job, views, chain.data, policy.fleet) : [];
  const counts = views.reduce((c, v) => ({ ...c, [v.status]: (c[v.status] ?? 0) + 1 }), {} as Partial<Record<EntryStatus, number>>);
  const accepting = policyAccepting(policy);
  // Claims cannot be registered under a policy that no longer accepts
  // enrollments; naming and grouping already-enrolled devices still can.
  // Each registered, not yet enrolled claim will take one of the organization's
  // device slots when its device enrolls. Do not pay to register claims that
  // cannot enroll: the contract refuses enrollment once the limit is reached.
  const awaiting = (counts["awaiting-device"] ?? 0) + (counts.confirming ?? 0);
  const toRegister = (counts.unregistered ?? 0) + (counts.unfunded ?? 0);
  const slotsLeft = freeSlots === undefined ? undefined : freeSlots - awaiting;
  const capacityShort = slotsLeft === undefined ? 0 : Math.max(0, toRegister - Math.max(0, slotsLeft));
  const runnable = accepting || capacityShort ? steps.filter((s) => s.kind === "bind") : steps;
  const quotaShort = (counts.unregistered ?? 0) + (counts.unfunded ?? 0) + (counts["awaiting-device"] ?? 0) - Number(policy.remaining);

  function record(action: string, count: number, digest?: string, error?: string) {
    const next = { ...jobRef.current, log: [...jobRef.current.log, { at: Date.now(), action, count, digest, error }].slice(-200) };
    jobRef.current = next; setJob(next);
  }
  function markEntries(claims: Set<string>, change: Partial<EnrollmentJob["entries"][number]>) {
    const next = { ...jobRef.current, entries: jobRef.current.entries.map((e) => claims.has(e.claim) ? { ...e, ...change } : e) };
    jobRef.current = next; setJob(next);
  }

  /**
   * One wallet approval per transaction-sized step, run in order. A failure
   * stops the run and is recorded against that step's entries; the next run
   * re-plans from fresh chain state.
   */
  async function runSteps(list: { label: string; entries: { claim: string }[]; build: () => Transaction; registers?: { claim: string }[] }[]) {
    if (running.current) return;
    running.current = true;
    stop.current = false;
    try {
      for (let i = 0; i < list.length && !stop.current; i++) {
        const step = list[i], claims = new Set(step.entries.map((e) => e.claim)), registers = new Set(step.registers?.map((e) => e.claim));
        let phase = "validating" as TransactionPhase;
        let definiteFailure = false;
        setProgress({ step: i + 1, total: list.length, label: step.label });
        if (registers.size) markEntries(registers, { submitted: true });
        try {
          const { digest } = await mutateAsync({ transaction: step.build(), onDefiniteFailure: () => { definiteFailure = true; }, onSubmitted: (digest) => { if (registers.size) markEntries(registers, { registrationDigest: digest }); }, onProgress: (p) => { phase = p; setProgress({ step: i + 1, total: list.length, label: step.label, phase: p }); } });
          markEntries(claims, { error: undefined });
          record(step.label, claims.size, digest);
        } catch (e) {
          const message = (e as Error).message;
          // Validation/refusal, a check that failed before anything was sent,
          // or explicit failed effects permit retry. Only an uncertain outcome
          // (possibly sent) must stay submitted.
          if (registers.size && (phase === "validating" || definiteFailure || transactionStage(e) !== "uncertain")) markEntries(registers, { submitted: undefined });
          markEntries(claims, { error: message });
          record(step.label, claims.size, undefined, message);
          onFailure(e, step.label);
          break;
        }
      }
    } finally {
      setProgress(null);
      running.current = false;
      await chain.refetch();
    }
  }

  const [deviceGas, setDeviceGas] = useState("0.05");
  function fundDevices() {
    const addresses = views.filter((v) => v.status === "complete" || v.status === "binding").map((v) => v.device!.deviceAddress);
    const mist = toMist(deviceGas);
    if (!addresses.length || !mist) return alert("Enter a positive SUI amount per device");
    if (!confirm(`Send ${deviceGas} SUI of ongoing gas to each of ${addresses.length} enrolled devices (${sui(mist * BigInt(addresses.length))} in total)?`)) return;
    runSteps(chunk(addresses, LIMITS.transfersPerTx).map((part, i, all) => ({ label: `Fund device gas ${i + 1}/${all.length}`, entries: [], build: () => buildTransferTx(part, mist) })));
  }
  function revokeUnused() {
    const unused = views.filter((v) => v.status === "awaiting-device" || v.status === "unfunded");
    if (!unused.length || !confirm(`Revoke ${unused.length} unused claims? Their credentials stop working permanently.`)) return;
    // Mark first: an interrupted revocation must never be re-registered automatically.
    markEntries(new Set(unused.map((v) => v.entry.claim)), { revoked: true });
    runSteps(chunk(unused, LIMITS.revokesPerTx).map((part, i, all) => ({ label: `Revoke claims ${i + 1}/${all.length}`, entries: part.map((v) => v.entry),
      build: () => buildRevokeClaimsTx(policy.id, part.map((v) => v.entry.claim), policy.fleet) })));
  }
  /** Revoke only the named unused claims whose credential file was lost. */
  function revokeLost(plan: LostCredentialPlan) {
    const lost = plan.revoke;
    if (!lost.length || !confirm(`Revoke ${lost.length} lost claim${lost.length === 1 ? "" : "s"} (${lost.map((v) => v.entry.serial).join(", ")})? Their credential files stop working permanently. Enrolled devices and other claims are not affected.`)) return;
    // Mark first: an interrupted revocation must never be re-registered automatically.
    markEntries(new Set(lost.map((v) => v.entry.claim)), { revoked: true });
    runSteps(chunk(lost, LIMITS.revokesPerTx).map((part, i, all) => ({ label: `Revoke lost claims ${i + 1}/${all.length}`, entries: part.map((v) => v.entry),
      build: () => buildRevokeClaimsTx(policy.id, part.map((v) => v.entry.claim), policy.fleet) })));
  }
  /** Free the serials of lost reservations that hold no live claim, so they can be reserved again. */
  function releaseLost(plan: LostCredentialPlan) {
    const claims = new Set(plan.release.map((v) => v.entry.claim));
    if (!claims.size || !confirm(`Remove ${claims.size} reservation${claims.size === 1 ? "" : "s"} (${plan.release.map((v) => v.entry.serial).join(", ")})? You can then reserve the same serial again and download a new credential file.`)) return;
    const next = { ...jobRef.current, entries: jobRef.current.entries.filter((e) => !claims.has(e.claim)) };
    jobRef.current = next; setJob(next);
  }
  function forgetReserved() {
    const reserved = new Set(views.filter((v) => v.status === "unregistered" || v.status === "withdrawn" || v.status === "revoked").map((v) => v.entry.claim));
    if (!reserved.size || !confirm(`Remove ${reserved.size} reservations that were never used? Their names and serials become available again.`)) return;
    setJob({ ...job, entries: job.entries.filter((e) => !reserved.has(e.claim)) });
  }
  async function recoverInterrupted() {
    setProgress({ step: 1, total: 1, label: "Checking interrupted submissions" });
    try {
      // Refresh before offering recovery; never reset a now-visible claim.
      const fresh = await chain.refetch();
      if (!fresh.data || fresh.isError) return onError("Refresh failed; cannot reset interrupted submissions");
      const current = jobRef.current;
      const recovered = recoverInterruptedRegistrations(current, fresh.data);
      const count = recovered.entries.filter((entry, i) => entry !== current.entries[i]).length;
      if (!count) return onError("No unproven interrupted submissions remain. Confirmed registrations cannot be reset here.");
      if (!confirm(`Reset ${count} interrupted submissions to reservations? Check your wallet history first and confirm they never executed. No digest was saved, so success cannot be ruled out. Running pending transactions again may pay registration funding TWICE. This does not revoke any on-chain claim.`)) return;
      jobRef.current = recovered; setJob(recovered);
    } finally { setProgress(null); }
  }
  function reRegisterWithdrawn() {
    const withdrawn = new Set(views.filter((v) => v.status === "withdrawn").map((v) => v.entry.claim));
    if (!withdrawn.size || !confirm(`Re-register ${withdrawn.size} withdrawn claims? Only do this if their credentials were not leaked.`)) return;
    markEntries(withdrawn, { submitted: undefined, registrationDigest: undefined, observedRegistered: undefined, observedPolicyVersion: undefined });
    chain.refetch();
  }
  function importJob(text: string) {
    if (!inventory.ready) return onError("Wait for your devices and groups to load, then import again.");
    try {
      const imported = parseJob(text, { operator, policyId: policy.id });
      const known = new Set(job.entries.map((e) => e.claim));
      const fresh = imported.entries.filter((e) => !known.has(e.claim));
      const check = checkReservations(fresh.map((e, i) => ({ line: i + 1, serial: e.serial, alias: e.alias, group: e.group ?? e.groupId })),
        { devices, groups, reserved: [...storedReservations(localStorage, job.operator), ...job.entries] });
      // Join by input row, never by serial: a rejected duplicate shares its serial with an accepted row.
      const added = check.accepted.map((a) => { const { group: _, ...e } = fresh[a.line - 1]; return { ...e, groupId: a.groupId }; });
      setJob({ ...job, claimFundMist: job.entries.length ? job.claimFundMist : imported.claimFundMist, entries: [...job.entries, ...added] });
      if (check.issues.length) onError(`Imported ${added.length}; skipped ${check.issues.length}: ${check.issues.slice(0, 5).map((i) => `${i.serial}: ${i.message}`).join("; ")}`);
    } catch (e) { onError(`Import failed: ${(e as Error).message}`); }
  }

  return (
    <>
      <AddReservations job={job} policy={policy} slotsLeft={slotsLeft === undefined ? undefined : Math.max(0, slotsLeft - toRegister)} devices={devices} groups={groups} inventory={inventory} onAdd={(entries, fund) =>
        setJob({ ...job, claimFundMist: fund, entries: [...job.entries, ...entries] })} onImport={importJob} />
      <section className="bg-white border border-gray-200 rounded p-4 space-y-3">
        <h2 className="font-semibold">3. Register and enroll</h2>
        {capacityShort > 0 && <p role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-red-800" data-testid="capacity-short">
          {capacityShort === toRegister ? `Registration is paused: your organization has no free device slots for ${toRegister === 1 ? "this device" : `these ${toRegister} devices`}.` : `Registration is paused: ${capacityShort} of ${toRegister} devices would exceed your organization's device limit.`}{" "}
          Raise the limit first; nothing is charged until then.</p>}
        {views.length > 0 && !capacityShort && <NextStep views={views} counts={counts} runnable={runnable} fund={BigInt(job.claimFundMist)} />}
        <div className="flex flex-wrap gap-2" data-testid="status-counts">
          {(Object.keys(STATUS_LABEL) as EntryStatus[]).filter((s) => counts[s]).map((s) =>
            <span key={s} className="px-2 py-0.5 rounded bg-gray-100">{STATUS_LABEL[s]}: {counts[s]}</span>)}
          {!views.length && <span className="text-gray-400">{chain.isLoading ? "Reading chain…" : "No devices added yet."}</span>}
        </div>
        {chain.error && <p role="alert" className="text-red-700">Chain read failed: {(chain.error as Error).message}</p>}
        {accepting && steps.some((s) => s.kind === "claims") && <p className="text-amber-700">This batch is {accepting}: devices cannot enroll and credentials cannot be registered until you change its limits.</p>}
        {quotaShort > 0 && <p className="text-amber-700">This batch allows {quotaShort} fewer enrollments than you have added; raise its quota.</p>}
        {progress && <div role="status" className="text-[#2e6fb0]">{progress.label} (transaction {progress.step} of {progress.total}){progress.phase === "wallet" ? " · approve in your wallet" : progress.phase === "confirming" ? " · confirming" : ""}</div>}
        <div className="flex flex-wrap gap-2">
          <button className={primary} disabled={!!progress || !runnable.length} onClick={() => runSteps(runnable)}>
            {runnable.length ? `Run ${runnable.length} pending transaction${runnable.length === 1 ? "" : "s"}` : "Nothing pending"}
          </button>
          {progress && <button className={button} onClick={() => { stop.current = true; }}>Stop after this transaction</button>}
          <button className={button} disabled={!!progress} onClick={() => chain.refetch()}>Refresh</button>
          {/* Recovery actions appear only when they apply. */}
          {views.some(v => v.status === "confirming" && !v.entry.registrationDigest && !v.entry.observedRegistered) &&
            <button className={button} disabled={!!progress} onClick={recoverInterrupted}>Reset interrupted submissions</button>}
          {!!counts.withdrawn && <button className={button} disabled={!!progress} onClick={reRegisterWithdrawn}>Re-register withdrawn claims</button>}
        </div>
        {!!(counts.complete || counts.binding) && <div className="flex flex-wrap gap-2 items-end border-t pt-3">
          <span className="text-gray-500">Devices pay a little gas for each status report. Send each enrolled device:</span>
          <label>SUI per device <input value={deviceGas} onChange={(e) => setDeviceGas(e.target.value)} className={`${input} w-20 ml-1`} /></label>
          <button className={button} disabled={!!progress} onClick={fundDevices}>Fund enrolled devices</button>
        </div>}
        <EntryTable views={views} groups={groups} />
        <details className="border-t pt-3" data-testid="more-actions">
          <summary className="cursor-pointer font-medium">More actions</summary>
          <div className="mt-2 flex flex-wrap gap-2">
            <button className={button} disabled={!!progress} onClick={revokeUnused}>Revoke unused claims</button>
            <button className={button} disabled={!!progress} onClick={forgetReserved}>Remove unused reservations</button>
            <button className={button} onClick={() => download(`suifleet-enrollment-job-${policy.id.slice(2, 10)}.json`, JSON.stringify(job, null, 2), "application/json")}>Export job</button>
          </div>
        </details>
        <LostCredentials views={views} busy={!!progress} onRevoke={revokeLost} onRelease={releaseLost} />
      </section>
    </>
  );
}

/** The organization's device slots; enrollment fails on chain once they are used up. */
function CapacityNotice({ active, limit }: { active: bigint; limit: bigint }) {
  const free = limit > active ? limit - active : BigInt(0);
  const checkout = !!MANIFEST?.type_origins["subscription::Account"] && !!MANIFEST?.subscription_coins;
  if (free > BigInt(0)) return <p role="status" className="text-gray-600" data-testid="device-capacity">Device slots: {active.toString()} of {limit.toString()} in use, {free.toString()} free. <Link href="/entitlements" className="underline">Device capacity</Link></p>;
  return <div role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-red-800 space-y-1" data-testid="device-capacity">
    <p className="font-medium">{limit === BigInt(0) ? "Your organization has no device slots yet." : `All ${limit.toString()} device slots are in use.`} Devices cannot enroll until you raise the limit.</p>
    <p>{checkout ? <><Link href="/billing" className="underline">Choose a plan</Link> or review <Link href="/entitlements" className="underline">Device capacity</Link>.</> : <>Ask your service administrator to raise it on <Link href="/entitlements" className="underline">Device capacity</Link>.</>} Credentials can be downloaded once a slot is free; nothing is charged before then.</p>
  </div>;
}

/** One sentence: what the operator should do now. */
function NextStep({ views, counts, runnable, fund }: { views: EntryView[]; counts: Partial<Record<EntryStatus, number>>; runnable: { kind: "claims" | "bind" }[]; fund: bigint }) {
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const toRegister = (counts.unregistered ?? 0) + (counts.unfunded ?? 0);
  const waiting = views.filter((v) => v.status === "awaiting-device");
  const box = "rounded border border-blue-200 bg-blue-50 px-3 py-2 text-[#1f4f80]";
  if (runnable.some((s) => s.kind === "claims") && toRegister)
    return <div className={box} data-testid="next-step"><b>Next:</b> register {plural(toRegister, "device credential")} with your wallet ({sui(fund)} enrollment gas each), so {toRegister === 1 ? "the device" : "the devices"} can enroll.</div>;
  if (runnable.some((s) => s.kind === "bind"))
    return <div className={box} data-testid="next-step"><b>Next:</b> {plural((counts.binding ?? 0), "device")} enrolled. Apply {(counts.binding ?? 0) === 1 ? "its name and group" : "their names and groups"} with your wallet.</div>;
  if (waiting.length) {
    const serial = waiting[0].entry.serial;
    return <div className={box} data-testid="next-step">
      <p><b>Next:</b> waiting for {plural(waiting.length, "device")} to enroll. Copy each device&apos;s own folder from the credential file onto it, then run on the device:</p>
      <pre className="mt-1 overflow-x-auto rounded bg-white px-2 py-1 text-xs text-gray-800">sudo suifleet --enroll enrollment/{waiting.length === 1 ? serial : "SERIAL"}/enroll.toml</pre>
      <p className="mt-1 text-xs">Refresh here afterwards. See <GuideLink section="enroll">first-boot enrollment</GuideLink> for setting up the client after enrollment.</p>
    </div>;
  }
  if (counts.complete === views.length) return <div className="rounded border border-green-200 bg-green-50 px-3 py-2 text-green-800" data-testid="next-step">All {plural(views.length, "device")} enrolled. They appear on the Devices page.</div>;
  return null;
}

type Inventory = { ready: boolean; error?: string; retry?: () => void };

function AddReservations({ job, policy, slotsLeft, devices, groups, inventory, onAdd, onImport }: {
  job: EnrollmentJob; policy: PolicyState; slotsLeft?: number; devices: DeviceObject[]; groups: DeviceGroup[]; inventory: Inventory;
  onAdd: (entries: EnrollmentJob["entries"], claimFundMist: string) => void; onImport: (text: string) => void;
}) {
  const [mode, setMode] = useState<AddMode>("pair");
  const [csv, setCsv] = useState("serial,alias,group\n");
  const [template, setTemplate] = useState("device-{n:4}");
  const [start, setStart] = useState(job.entries.length + 1);
  const [alias, setAlias] = useState("");
  const [serial, setSerial] = useState("");
  const [groupId, setGroupId] = useState("");
  const [fund, setFund] = useState((Number(job.claimFundMist) / 1e9).toString());
  const [downloaded, setDownloaded] = useState(0);
  // The archive is the only copy of its claim keys: confirm before each download.
  const [acknowledged, setAcknowledged] = useState(false);
  const [configTemplate, setConfigTemplate] = useState("");
  useEffect(() => { setAcknowledged(false); }, [mode, csv, template, start, alias, serial, groupId]);

  // Checked as the operator types; nothing is sent anywhere.
  const check = useMemo<ReservationCheck | null>(() => {
    if (!inventory.ready || mode === "roster") return null;
    const parsed = mode === "pair"
      ? { rows: serial.trim() ? [{ line: 1, serial: serial.trim(), alias: alias.trim() || serial.trim() }] : [], issues: [] }
      : parseRoster(csv, template, start);
    if (!parsed.rows.length && !parsed.issues.length) return null;
    const result = checkReservations(parsed.rows, { devices, groups, defaultGroupId: groupId || undefined,
      reserved: [...storedReservations(localStorage), ...job.entries] });
    return { accepted: result.accepted, issues: [...parsed.issues, ...result.issues] };
  }, [inventory.ready, mode, serial, alias, csv, template, start, devices, groups, groupId, job.entries]);

  function generate() {
    if (!check?.accepted.length || !acknowledged || overCapacity) return;
    const mist = toMist(fund);
    if (!mist || mist > MIST / BigInt(10)) return alert("Enrollment gas per device must be more than 0 and at most 0.1 SUI");
    const { entries, credentials } = generateCredentials(check.accepted);
    // The archive is the only copy of the claim keys: they are not stored.
    download(`suifleet-enrollment-${policy.id.slice(2, 10)}-${Date.now()}.tar`, credentialArchive(credentials, policy.id, JSON.stringify(requireManifest(), null, 2), undefined, policy.fleet, configTemplate.trim() ? configTemplate : undefined), "application/x-tar");
    credentials.length = 0;
    onAdd(entries, mist.toString());
    setDownloaded(entries.length);
    setStart(start + entries.length);
    setAcknowledged(false); setAlias(""); setSerial("");
  }

  const count = check?.accepted.length ?? 0;
  const overCapacity = slotsLeft !== undefined && count > slotsLeft;
  return (
    <section className="bg-white border border-gray-200 rounded p-4 space-y-3">
      <h2 className="font-semibold">2. Add devices</h2>
      <div className="flex gap-2" role="tablist">
        {([["pair", "Pair one device"], ["csv", "Import CSV"], ["roster", "Import roster file"]] as [AddMode, string][]).map(([m, label]) =>
          <button key={m} role="tab" aria-selected={mode === m} className={mode === m ? primary : button} onClick={() => setMode(m)}>{label}</button>)}
      </div>
      {mode === "roster" ? (
        <div className="space-y-2">
          <p className="text-gray-500">For large batches, generate claim keys offline with <code>scripts/claims.py generate --roster devices.csv</code> and import its public <code>roster.json</code>, or re-import an exported job. Files containing keys are refused.</p>
          <input type="file" accept=".json,application/json" aria-label="Roster or job file" disabled={!inventory.ready} onChange={async (e) => { const f = e.target.files?.[0]; if (f) onImport(await f.text()); e.target.value = ""; }} />
        </div>
      ) : (
        <div className="space-y-3">
          {mode === "csv" ? <>
            <p className="text-gray-500">One device per line. <code>serial</code> is required; <code>alias</code> (its name) and <code>group</code> are optional. Blank names come from the naming template: <code>{"{n:4}"}</code> is the row number padded to 4 digits, <code>{"{serial}"}</code> the serial.</p>
            <textarea aria-label="Roster CSV" value={csv} onChange={(e) => setCsv(e.target.value)} rows={6} className={`${input} w-full font-mono`} />
            <div className="flex flex-wrap gap-2 items-end">
              <input type="file" accept=".csv,text/csv" aria-label="Roster CSV file" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setCsv(await f.text()); e.target.value = ""; }} />
              <label>Naming template <input aria-label="Naming template" value={template} onChange={(e) => setTemplate(e.target.value)} className={`${input} w-40 ml-1 font-mono`} /></label>
              <label>First number <input type="number" min={0} value={start} onChange={(e) => setStart(Number(e.target.value))} className={`${input} w-24 ml-1`} /></label>
            </div>
          </> : <div className="flex flex-wrap gap-3 items-end">
            <label>Serial number <input aria-label="Serial number" value={serial} onChange={(e) => setSerial(e.target.value)} placeholder="SN-000123" className={`${input} ml-1`} /></label>
            <label>Name <span className="text-gray-400">(optional)</span> <input aria-label="Name" value={alias} onChange={(e) => setAlias(e.target.value)} placeholder={serial.trim() || "same as serial"} className={`${input} ml-1`} /></label>
          </div>}
          <div className="flex flex-wrap gap-3 items-end">
            <label>Group {mode === "csv" ? "(rows without one)" : ""}
              <select aria-label="Default group" value={groupId} onChange={(e) => setGroupId(e.target.value)} className={`${input} ml-1`}>
                <option value="">No group</option>
                {groups.map((g) => <option key={g.objectId} value={g.objectId}>{g.name}</option>)}
              </select>
            </label>
            {!inventory.ready && !inventory.error && <span role="status" className="text-gray-500">Loading devices and groups…</span>}
          </div>
          {inventory.error && <p role="alert" className="text-red-700">{inventory.error} <button className="underline" onClick={inventory.retry}>Retry</button></p>}
          {check && <div data-testid="reservation-check" className="space-y-1">
            {mode === "csv" ? <p>{check.accepted.length} ready{check.issues.length ? `, ${check.issues.length} rejected` : ""}.</p>
              : check.accepted[0] && <p className="text-green-700">Ready: {check.accepted[0].alias}{check.accepted[0].alias !== check.accepted[0].serial ? ` (serial ${check.accepted[0].serial})` : ""}</p>}
            {check.accepted.length > BROWSER_CREDENTIAL_LIMIT && <p className="text-amber-700">The browser generates at most {BROWSER_CREDENTIAL_LIMIT} credentials at once; use scripts/claims.py for larger batches.</p>}
            {check.issues.slice(0, 50).map((i) => <p key={`${i.line}-${i.message}`} className="text-red-700 text-xs">{mode === "csv" ? `Line ${i.line}${i.serial ? ` (${i.serial})` : ""}: ` : ""}{i.message}</p>)}
          </div>}
          <details>
            <summary className="cursor-pointer text-gray-500">Advanced</summary>
            <label className="mt-2 block">Enrollment gas per device (SUI) <input aria-label="Enrollment gas per claim" value={fund} onChange={(e) => setFund(e.target.value)} className={`${input} w-20 ml-1`} />
              <span className="text-gray-500"> pays only for the device&apos;s enrollment transaction.</span></label>
            <label className="mt-2 block text-sm">
              Batch configuration template (optional)
              <textarea className={`${input} mt-1 block w-full font-mono`} rows={4} value={configTemplate}
                onChange={(event) => { setConfigTemplate(event.target.value); setAcknowledged(false); }} />
            </label>
          </details>
          <div role="note" className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 space-y-1" data-testid="credential-warning">
            <p><b>This file is the only copy of the credentials;</b> SuiFleet cannot download it again. Give each device only its own <code>enrollment/SERIAL/</code> folder and delete the file once devices have enrolled. Lost it before a device enrolled? Use “Lost a credential file?” in step 3.</p>
            <label className="flex items-center gap-2 font-medium"><input type="checkbox" checked={acknowledged} disabled={!count || overCapacity} onChange={(e) => setAcknowledged(e.target.checked)} /> I understand this download is the only copy of these credentials</label>
          </div>
          {overCapacity && <p role="alert" className="text-red-700" data-testid="capacity-download">{slotsLeft === 0 ? "No device slots are free" : `Only ${slotsLeft} device slot${slotsLeft === 1 ? " is" : "s are"} free`} for new devices. Raise your device limit above, then download.</p>}
          <button className={primary} disabled={!count || !acknowledged || overCapacity} onClick={generate}>
            {count ? `Download credentials for ${count} device${count === 1 ? "" : "s"}` : "Download credentials"}
          </button>
          {downloaded > 0 && !count && <p role="status" className="text-green-700">Downloaded credentials for {downloaded} device{downloaded === 1 ? "" : "s"}. Next: register {downloaded === 1 ? "it" : "them"} in step 3.</p>}
        </div>
      )}
    </section>
  );
}

const SHOWN = 200;

function LostCredentials({ views, busy, onRevoke, onRelease }: { views: EntryView[]; busy: boolean; onRevoke: (plan: LostCredentialPlan) => void; onRelease: (plan: LostCredentialPlan) => void }) {
  const [text, setText] = useState("");
  const serials = parseSerials(text);
  // Recomputed from current chain state on every render, never a stale snapshot.
  const plan = serials.length ? planLostCredentials(views, serials) : null;
  const list = (items: EntryView[]) => items.map((v) => v.entry.serial).join(", ");
  return (
    <details className="border-t pt-3" data-testid="lost-credentials">
      <summary className="cursor-pointer font-medium">Lost a credential file?</summary>
      <div className="mt-2 space-y-2">
        <p className="text-gray-500">A lost file cannot be downloaded again. Revoke each unused claim it contained so nobody can enroll with it, remove the reservation, then reserve the same serial again to download a new file. Devices that already enrolled are unaffected and must not be given new credentials.</p>
        <label className="block">Serial numbers from the lost file
          <textarea aria-label="Lost credential serials" value={text} onChange={(e) => setText(e.target.value)} rows={2} className={`${input} block w-full font-mono`} placeholder="SN-000123, SN-000124" />
        </label>
        {plan && <div className="space-y-1" role="status">
          {plan.revoke.length > 0 && <p>Revoke first: {list(plan.revoke)}. <button className={button} disabled={busy} onClick={() => onRevoke(plan)}>Revoke {plan.revoke.length} lost claim{plan.revoke.length === 1 ? "" : "s"}</button></p>}
          {plan.release.length > 0 && <p>No live claim; ready to reissue: {list(plan.release)}. <button className={button} disabled={busy} onClick={() => onRelease(plan)}>Remove {plan.release.length} reservation{plan.release.length === 1 ? "" : "s"} to reissue</button></p>}
          {plan.enrolled.length > 0 && <p className="text-green-700">Already enrolled; nothing to do: {list(plan.enrolled)}. The device keeps its own key; do not issue a new credential.</p>}
          {plan.wait.length > 0 && <p className="text-amber-700">Registration not confirmed yet: {list(plan.wait)}. Refresh and wait for it to confirm before revoking.</p>}
          {plan.blocked.length > 0 && <p className="text-red-700">Needs review before any change: {list(plan.blocked)}.</p>}
          {plan.unknown.length > 0 && <p className="text-gray-500">Not in this job: {plan.unknown.join(", ")}.</p>}
          {plan.revoke.length > 0 && <p className="text-xs text-gray-500">After revocation confirms, the serial shows as Revoked here; remove its reservation, then reserve it again in step 2.</p>}
        </div>}
      </div>
    </details>
  );
}

function EntryTable({ views, groups }: { views: EntryView[]; groups: { objectId: string; name: string }[] }) {
  const [filter, setFilter] = useState<EntryStatus | "">("");
  const [technical, setTechnical] = useState(false);
  const shown = views.filter((v) => !filter || v.status === filter);
  if (!views.length) return null;
  return (
    <div className="space-y-2">
      <div className="flex gap-3 items-center">
        <label>Show <select aria-label="Status filter" value={filter} onChange={(e) => setFilter(e.target.value as EntryStatus | "")} className={`${input} ml-1`}>
          <option value="">All</option>
          {(Object.keys(STATUS_LABEL) as EntryStatus[]).map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
        </select></label>
        <label className="flex items-center gap-1"><input type="checkbox" checked={technical} onChange={(e) => setTechnical(e.target.checked)} /> Technical details</label>
        {shown.length > SHOWN && <span className="text-gray-500">First {SHOWN} of {shown.length}</span>}
      </div>
      <table className="w-full text-sm" data-testid="enrollment-entries">
        <thead><tr className="text-left text-xs text-gray-500 border-b">
          {["Alias", "Serial", "Group", "Status", "Details", ...(technical ? ["Claim", "Device address"] : [])].map((h) => <th key={h} className="py-1 pr-3 font-medium">{h}</th>)}
        </tr></thead>
        <tbody className="divide-y divide-gray-100">
          {shown.slice(0, SHOWN).map((v) => <tr key={v.entry.claim}>
            <td className="py-1 pr-3 font-medium">{v.entry.alias}</td>
            <td className="py-1 pr-3">{v.entry.serial}</td>
            <td className="py-1 pr-3">{v.entry.groupId ? groups.find((g) => g.objectId === v.entry.groupId)?.name ?? "—" : "—"}</td>
            <td className="py-1 pr-3">{STATUS_LABEL[v.status]}</td>
            <td className="py-1 pr-3 text-xs text-gray-500">{[v.detail, v.entry.error && `last error: ${v.entry.error}`].filter(Boolean).join(" · ")}</td>
            {technical && <>
              <td className="py-1 pr-3 font-mono text-xs text-gray-400">{v.entry.claim.slice(0, 12)}…</td>
              <td className="py-1 pr-3 font-mono text-xs text-gray-400">{v.enrollment ? `${v.enrollment.deviceAddress.slice(0, 12)}…` : "—"}</td>
            </>}
          </tr>)}
        </tbody>
      </table>
    </div>
  );
}
