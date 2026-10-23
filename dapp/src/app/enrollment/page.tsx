"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { PageHeader } from "@/components/ui";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { useFleetTransaction, type TransactionPhase } from "@/hooks/useFleetTransaction";
import { NETWORK } from "@/lib/constants";
import { requireManifest } from "@/lib/deployment-manifest";
import {
  BROWSER_CREDENTIAL_LIMIT, LIMITS, buildClosePolicyTx, buildCreatePolicyTx, buildRevokeClaimsTx, buildSetLimitsTx, buildTransferTx,
  checkReservations, chunk, createdPolicy, credentialArchive, entryStatuses, fetchPolicies, generateCredentials, loadJob, newJob,
  parseJob, parseRoster, plan, policyAccepting, saveJob, snapshot, storedReservations,
  type EnrollmentJob, type EntryStatus, type EntryView, type PolicyState, type ReservationCheck,
} from "@/lib/enrollment";
import type { DeviceGroup, DeviceObject } from "@/lib/sui";
import type { Transaction } from "@mysten/sui/transactions";

const STATUS_LABEL: Record<EntryStatus, string> = {
  unregistered: "Reserved", unfunded: "Needs enrollment gas", "awaiting-device": "Waiting for device", revoked: "Revoked",
  withdrawn: "Claim withdrawn", binding: "Enrolled, naming", complete: "Enrolled", conflict: "Conflict", untrusted: "Not yours",
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
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const { mutateAsync } = useFleetTransaction();
  const [selected, setSelected] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const policies = useQuery({ queryKey: ["enrollment-policies", NETWORK, account?.address, selected], queryFn: () => fetchPolicies(client, account!.address, selected ? [selected] : []),
    enabled: !!account?.address, refetchInterval: 30_000 });
  const policy = policies.data?.find((p) => p.id === selected);

  async function run(transaction: Transaction) {
    setBusy(true); setError("");
    try { return await mutateAsync({ transaction }); }
    catch (e) { setError((e as Error).message); return undefined; }
    finally { setBusy(false); }
  }

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const days = Number(fd.get("days"));
    const result = await run(buildCreatePolicyTx(BigInt(fd.get("quota") as string), days > 0 ? BigInt(Date.now() + days * 86_400_000) : BigInt(0)));
    if (!result) return;
    setSelected(await createdPolicy(client, result.digest));
    await policies.refetch();
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet to enroll devices.</p>;
  return (
    <div className="space-y-4 max-w-5xl text-sm">
      <PageHeader title="Enrollment" subtitle="Reserve device names and serial numbers; devices enroll themselves with single-use credentials" />
      {error && <p role="alert" className="text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">{error}</p>}
      <section className="bg-white border border-gray-200 rounded p-4 space-y-3">
        <h2 className="font-semibold">1. Enrollment batch policy</h2>
        <p className="text-gray-500">A policy limits how many devices may enroll and until when. Each reservation gets one single-use claim; you can revoke unused claims or close the policy at any time.</p>
        <div className="flex flex-wrap gap-2 items-center">
          <label htmlFor="policy">Policy</label>
          <select id="policy" className={input} value={selected} onChange={(e) => setSelected(e.target.value)}>
            <option value="">Select a policy…</option>
            {policies.data?.map((p) => <option key={p.id} value={p.id}>{policyLabel(p)}</option>)}
          </select>
        </div>
        <form onSubmit={create} className="flex flex-wrap gap-2 items-end">
          <label>Quota <input name="quota" type="number" min={1} defaultValue={100} required className={`${input} w-28 ml-1`} /></label>
          <label>Valid for days (0 = no expiry) <input name="days" type="number" min={0} defaultValue={14} required className={`${input} w-20 ml-1`} /></label>
          <button type="submit" disabled={busy} className={primary}>Create policy</button>
        </form>
        {policy && <PolicyControls policy={policy} busy={busy} run={async (tx) => { if (await run(tx)) await policies.refetch(); }} />}
      </section>
      {policy && <JobPanel key={policy.id} policy={policy} operator={account.address} onError={setError} />}
    </div>
  );
}

function policyLabel(p: PolicyState) {
  const state = policyAccepting(p);
  return `${p.id.slice(0, 10)}… · ${p.enrolled} enrolled · ${p.remaining} left · ${p.unusedClaims} unused claims${state ? ` · ${state}` : ""}`;
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
      {policy.open && <div className="flex flex-wrap gap-2 items-end">
        <label>Remaining quota <input value={quota} onChange={(e) => setQuota(e.target.value)} type="number" min={0} className={`${input} w-28 ml-1`} /></label>
        <label>Valid for days from now (0 = no expiry) <input value={days} onChange={(e) => setDays(e.target.value)} type="number" min={0} className={`${input} w-20 ml-1`} /></label>
        <button disabled={busy} className={button} onClick={() => run(buildSetLimitsTx(policy.id, BigInt(quota || 0), Number(days) > 0 ? BigInt(Date.now() + Number(days) * 86_400_000) : BigInt(0)))}>Set limits</button>
        <button disabled={busy} className={`${button} text-red-600`} onClick={() => confirm("Close this policy permanently? No device can enroll under it afterwards.") && run(buildClosePolicyTx(policy.id))}>Close policy</button>
      </div>}
    </div>
  );
}

type AddMode = "csv" | "pair" | "roster";

function JobPanel({ policy, operator, onError }: { policy: PolicyState; operator: string; onError: (message: string) => void }) {
  const client = useCurrentClient();
  const { data: devices = [] } = useDevices();
  const { data: groups = [] } = useGroups();
  const { mutateAsync } = useFleetTransaction();
  const [job, setJobState] = useState<EnrollmentJob>(() => {
    try { return loadJob(localStorage, operator, policy.id) ?? newJob(operator, policy.id); }
    catch (e) { onError(`Stored job unreadable: ${(e as Error).message}`); return newJob(operator, policy.id); }
  });
  const setJob = (next: EnrollmentJob) => { saveJob(localStorage, next); setJobState(next); };
  const jobRef = useRef(job); jobRef.current = job;
  const [progress, setProgress] = useState<{ step: number; total: number; label: string; phase?: TransactionPhase } | null>(null);
  const stop = useRef(false);
  const chain = useQuery({
    queryKey: ["enrollment-job", NETWORK, policy.id, job.entries.length],
    queryFn: () => snapshot(client, jobRef.current),
    refetchInterval: 15_000,
  });
  const views = useMemo(() => chain.data ? entryStatuses(job, chain.data) : [], [job, chain.data]);
  const steps = chain.data ? plan(job, views, chain.data) : [];
  const counts = views.reduce((c, v) => ({ ...c, [v.status]: (c[v.status] ?? 0) + 1 }), {} as Partial<Record<EntryStatus, number>>);
  const accepting = policyAccepting(policy);
  // Claims cannot be registered under a policy that no longer accepts
  // enrollments; naming and grouping already-enrolled devices still can.
  const runnable = accepting ? steps.filter((s) => s.kind === "bind") : steps;
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
    stop.current = false;
    try {
      for (let i = 0; i < list.length && !stop.current; i++) {
        const step = list[i], claims = new Set(step.entries.map((e) => e.claim)), registers = new Set(step.registers?.map((e) => e.claim));
        let phase = "validating" as TransactionPhase;
        setProgress({ step: i + 1, total: list.length, label: step.label });
        if (registers.size) markEntries(registers, { submitted: true });
        try {
          const { digest } = await mutateAsync({ transaction: step.build(), onProgress: (p) => { phase = p; setProgress({ step: i + 1, total: list.length, label: step.label, phase: p }); } });
          markEntries(claims, { error: undefined });
          record(step.label, claims.size, digest);
        } catch (e) {
          const message = (e as Error).message;
          // Refused before or by the wallet: nothing executed, so these claims
          // return to plain reservations. After the wallet returned a result
          // (confirming), the outcome is the chain's and they stay submitted.
          if (registers.size && phase !== "confirming") markEntries(registers, { submitted: undefined });
          markEntries(claims, { error: message });
          record(step.label, claims.size, undefined, message);
          onError(`${step.label}: ${message}`);
          break;
        }
      }
    } finally {
      setProgress(null);
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
      build: () => buildRevokeClaimsTx(policy.id, part.map((v) => v.entry.claim)) })));
  }
  function forgetReserved() {
    const reserved = new Set(views.filter((v) => v.status === "unregistered" || v.status === "withdrawn" || v.status === "revoked").map((v) => v.entry.claim));
    if (!reserved.size || !confirm(`Remove ${reserved.size} reservations that were never used? Their names and serials become available again.`)) return;
    setJob({ ...job, entries: job.entries.filter((e) => !reserved.has(e.claim)) });
  }
  function reRegisterWithdrawn() {
    const withdrawn = new Set(views.filter((v) => v.status === "withdrawn").map((v) => v.entry.claim));
    if (!withdrawn.size || !confirm(`Re-register ${withdrawn.size} withdrawn claims? Only do this if their credentials were not leaked.`)) return;
    markEntries(withdrawn, { submitted: undefined });
    chain.refetch();
  }
  function importJob(text: string) {
    try {
      const imported = parseJob(text, { operator, policyId: policy.id });
      const known = new Set(job.entries.map((e) => e.claim));
      const fresh = imported.entries.filter((e) => !known.has(e.claim));
      const check = checkReservations(fresh.map((e, i) => ({ line: i + 1, serial: e.serial, alias: e.alias, group: e.group ?? e.groupId })),
        { devices, groups, reserved: [...storedReservations(localStorage), ...job.entries] });
      // Join by input row, never by serial: a rejected duplicate shares its serial with an accepted row.
      const added = check.accepted.map((a) => { const { group: _, ...e } = fresh[a.line - 1]; return { ...e, groupId: a.groupId }; });
      setJob({ ...job, claimFundMist: job.entries.length ? job.claimFundMist : imported.claimFundMist, entries: [...job.entries, ...added] });
      if (check.issues.length) onError(`Imported ${added.length}; skipped ${check.issues.length}: ${check.issues.slice(0, 5).map((i) => `${i.serial}: ${i.message}`).join("; ")}`);
    } catch (e) { onError(`Import failed: ${(e as Error).message}`); }
  }

  return (
    <>
      <AddReservations job={job} policy={policy} devices={devices} groups={groups} onAdd={(entries, fund) =>
        setJob({ ...job, claimFundMist: fund, entries: [...job.entries, ...entries] })} onImport={importJob} />
      <section className="bg-white border border-gray-200 rounded p-4 space-y-3">
        <h2 className="font-semibold">3. Register claims, then devices enroll themselves</h2>
        <p className="text-gray-500">
          Each claim sponsors only its own enrollment transaction ({sui(BigInt(job.claimFundMist))} each). On first boot the device runs{" "}
          <code>suifleet --enroll /path/to/enroll.toml</code> with its own device key. Once its enrollment is on chain, the alias, serial and group are applied here.
          Work is split into transactions of at most {LIMITS.claimsPerTx} claims or {LIMITS.bindsPerTx} devices; each needs one wallet approval.
        </p>
        <div className="flex flex-wrap gap-2" data-testid="status-counts">
          {(Object.keys(STATUS_LABEL) as EntryStatus[]).filter((s) => counts[s]).map((s) =>
            <span key={s} className="px-2 py-0.5 rounded bg-gray-100">{STATUS_LABEL[s]}: {counts[s]}</span>)}
          {!views.length && <span className="text-gray-400">{chain.isLoading ? "Reading chain…" : "No reservations yet."}</span>}
        </div>
        {chain.error && <p role="alert" className="text-red-700">Chain read failed: {(chain.error as Error).message}</p>}
        {accepting && steps.some((s) => s.kind === "claims") && <p className="text-amber-700">The policy is {accepting}: devices cannot enroll and claims cannot be registered until you change its limits.</p>}
        {quotaShort > 0 && <p className="text-amber-700">The policy allows {quotaShort} fewer enrollments than you have reserved; raise its quota.</p>}
        {progress && <div role="status" className="text-[#2e6fb0]">{progress.label} (transaction {progress.step} of {progress.total}){progress.phase === "wallet" ? " · approve in your wallet" : progress.phase === "confirming" ? " · confirming" : ""}</div>}
        <div className="flex flex-wrap gap-2">
          <button className={primary} disabled={!!progress || !runnable.length} onClick={() => runSteps(runnable)}>
            {runnable.length ? `Run ${runnable.length} pending transaction${runnable.length === 1 ? "" : "s"}` : "Nothing pending"}
          </button>
          {progress && <button className={button} onClick={() => { stop.current = true; }}>Stop after this transaction</button>}
          <button className={button} disabled={!!progress} onClick={() => chain.refetch()}>Refresh</button>
          <button className={button} disabled={!!progress} onClick={revokeUnused}>Revoke unused claims</button>
          <button className={button} disabled={!!progress || !counts.withdrawn} onClick={reRegisterWithdrawn}>Re-register withdrawn claims</button>
          <button className={button} disabled={!!progress} onClick={forgetReserved}>Remove unused reservations</button>
          <button className={button} onClick={() => download(`suifleet-enrollment-job-${policy.id.slice(2, 10)}.json`, JSON.stringify(job, null, 2), "application/json")}>Export job</button>
        </div>
        <div className="flex flex-wrap gap-2 items-end border-t pt-3">
          <span className="text-gray-500">Ongoing device gas is separate from enrollment sponsorship:</span>
          <label>SUI per device <input value={deviceGas} onChange={(e) => setDeviceGas(e.target.value)} className={`${input} w-20 ml-1`} /></label>
          <button className={button} disabled={!!progress || !(counts.complete || counts.binding)} onClick={fundDevices}>Fund enrolled devices</button>
        </div>
        <EntryTable views={views} groups={groups} />
      </section>
    </>
  );
}

function AddReservations({ job, policy, devices, groups, onAdd, onImport }: {
  job: EnrollmentJob; policy: PolicyState; devices: DeviceObject[]; groups: DeviceGroup[];
  onAdd: (entries: EnrollmentJob["entries"], claimFundMist: string) => void; onImport: (text: string) => void;
}) {
  const [mode, setMode] = useState<AddMode>("csv");
  const [csv, setCsv] = useState("serial,alias,group\n");
  const [template, setTemplate] = useState("device-{n:4}");
  const [start, setStart] = useState(job.entries.length + 1);
  const [alias, setAlias] = useState("");
  const [serial, setSerial] = useState("");
  const [groupId, setGroupId] = useState("");
  const [fund, setFund] = useState((Number(job.claimFundMist) / 1e9).toString());
  const [check, setCheck] = useState<ReservationCheck & { parseIssues: number } | null>(null);
  useEffect(() => setCheck(null), [mode, csv, template, start, alias, serial, groupId]);

  function validate() {
    const parsed = mode === "pair" ? { rows: [{ line: 1, serial: serial.trim(), alias: alias.trim() }], issues: [] } : parseRoster(csv, template, start);
    const result = checkReservations(parsed.rows, { devices, groups, defaultGroupId: groupId || undefined,
      reserved: [...storedReservations(localStorage), ...job.entries] });
    setCheck({ accepted: result.accepted, issues: [...parsed.issues, ...result.issues], parseIssues: parsed.issues.length });
  }
  function generate() {
    if (!check?.accepted.length) return;
    const mist = toMist(fund);
    if (!mist || mist > MIST / BigInt(10)) return alert("Enrollment gas per claim must be more than 0 and at most 0.1 SUI");
    const { entries, credentials } = generateCredentials(check.accepted);
    // The archive is the only copy of the claim keys: they are not stored.
    download(`suifleet-enrollment-${policy.id.slice(2, 10)}-${Date.now()}.tar`, credentialArchive(credentials, policy.id, JSON.stringify(requireManifest(), null, 2)), "application/x-tar");
    credentials.length = 0;
    onAdd(entries, mist.toString());
    setStart(start + entries.length);
    setCheck(null); setAlias(""); setSerial("");
  }

  return (
    <section className="bg-white border border-gray-200 rounded p-4 space-y-3">
      <h2 className="font-semibold">2. Reserve names and serial numbers</h2>
      <div className="flex gap-2" role="tablist">
        {([["csv", "Import CSV"], ["pair", "Pair one device"], ["roster", "Import roster file"]] as [AddMode, string][]).map(([m, label]) =>
          <button key={m} role="tab" aria-selected={mode === m} className={mode === m ? primary : button} onClick={() => setMode(m)}>{label}</button>)}
      </div>
      {mode === "roster" ? (
        <div className="space-y-2">
          <p className="text-gray-500">For large batches, generate claim keys offline with <code>scripts/claims.py generate --roster devices.csv</code> and import its public <code>roster.json</code>, or re-import an exported job. Files containing keys are refused.</p>
          <input type="file" accept=".json,application/json" aria-label="Roster or job file" onChange={async (e) => { const f = e.target.files?.[0]; if (f) onImport(await f.text()); e.target.value = ""; }} />
        </div>
      ) : (
        <div className="space-y-2">
          {mode === "csv" ? <>
            <p className="text-gray-500">Columns: <code>serial</code> (required), <code>alias</code> and <code>group</code> (a group name or ID). Blank aliases come from the naming template: <code>{"{n:4}"}</code> is the row number zero-padded to 4 digits, <code>{"{serial}"}</code> the serial.</p>
            <textarea aria-label="Roster CSV" value={csv} onChange={(e) => setCsv(e.target.value)} rows={6} className={`${input} w-full font-mono`} />
            <div className="flex flex-wrap gap-2 items-end">
              <input type="file" accept=".csv,text/csv" aria-label="Roster CSV file" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setCsv(await f.text()); e.target.value = ""; }} />
              <label>Naming template <input aria-label="Naming template" value={template} onChange={(e) => setTemplate(e.target.value)} className={`${input} w-40 ml-1 font-mono`} /></label>
              <label>First number <input type="number" min={0} value={start} onChange={(e) => setStart(Number(e.target.value))} className={`${input} w-24 ml-1`} /></label>
            </div>
          </> : <div className="flex flex-wrap gap-2 items-end">
            <p className="text-gray-500 w-full">Reserve a name for one device. You download its enrollment files; the device generates its own key and proves it when it enrolls. No address is needed.</p>
            <label>Alias <input aria-label="Alias" value={alias} onChange={(e) => setAlias(e.target.value)} placeholder="pi-kitchen" className={`${input} ml-1`} /></label>
            <label>Serial number <input aria-label="Serial number" value={serial} onChange={(e) => setSerial(e.target.value)} placeholder="SN-000123" className={`${input} ml-1`} /></label>
          </div>}
          <div className="flex flex-wrap gap-2 items-end">
            <label>Group {mode === "csv" ? "(rows without one)" : ""}
              <select aria-label="Default group" value={groupId} onChange={(e) => setGroupId(e.target.value)} className={`${input} ml-1`}>
                <option value="">No group</option>
                {groups.map((g) => <option key={g.objectId} value={g.objectId}>{g.name}</option>)}
              </select>
            </label>
            <label>Enrollment gas per claim (SUI) <input aria-label="Enrollment gas per claim" value={fund} onChange={(e) => setFund(e.target.value)} className={`${input} w-20 ml-1`} /></label>
            <button className={button} onClick={validate}>Check</button>
            <button className={primary} disabled={!check?.accepted.length} onClick={generate}>
              {check ? `Download credentials for ${check.accepted.length} device${check.accepted.length === 1 ? "" : "s"}` : "Download credentials"}
            </button>
          </div>
          {check && <div data-testid="reservation-check" className="space-y-1">
            <p>{check.accepted.length} ready{check.issues.length ? `, ${check.issues.length} rejected` : ""}.{check.accepted.length > BROWSER_CREDENTIAL_LIMIT && ` The browser generates at most ${BROWSER_CREDENTIAL_LIMIT}; use scripts/claims.py.`}</p>
            {check.issues.slice(0, 50).map((i) => <p key={`${i.line}-${i.message}`} className="text-red-700 text-xs">Line {i.line}{i.serial ? ` (${i.serial})` : ""}: {i.message}</p>)}
          </div>}
          <p className="text-xs text-gray-500">The download is the only copy of the claim keys. Put each <code>enrollment/SERIAL/</code> folder on that device&apos;s private provisioning media, keep the archive offline and delete it once devices have enrolled.</p>
        </div>
      )}
    </section>
  );
}

const SHOWN = 200;

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
