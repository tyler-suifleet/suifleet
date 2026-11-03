"use client";
import { SecondPersonSetup } from "./SecondPersonSetup";
import { SecondPersonApprovals } from "./SecondPersonApprovals";
import { MultisigAdministrator } from "./MultisigAdministrator";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { AVAILABLE_SECURITY_FACTORS, SECURITY_ACTIONS, SECURITY_FACTORS, buildSecurityPolicyTx, fetchOrganizationSecurity, securitySupported } from "@/lib/security-policy";
import { buildCancelSecurityTx, buildDelayConfigurationTx, buildExecuteSecurityEditTx, buildQueueSecurityTx, currentCustomerBytes, delayStatus, delaySupported, editArguments, fetchQueuedSecurityActions, fetchSecurityDelays, validateDelays, type SecurityEdit } from "@/lib/security-delay";

const styles = { workspace: "organization-security", setup: "organization-security-setup", setupBody: "organization-security-setup-body" };

export function OrganizationSecurity() {
  const selection = useFleetSelection(), client = useCurrentClient(), account = useCurrentAccount(), mutation = useFleetTransaction();
  const fleet = selection.fleet;
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => BigInt(Date.now()));
  useEffect(() => { const timer = setInterval(() => setNow(BigInt(Date.now())), 1000); return () => clearInterval(timer); }, []);
  const policy = useQuery({ queryKey: ["organization-security", fleet?.customerId], enabled: !!fleet && securitySupported(), queryFn: async () => (await fetchOrganizationSecurity(client, fleet!.customerId)) ?? null, refetchInterval: 10000 });
  const timing = useQuery({ queryKey: ["organization-delays", fleet?.customerId, policy.data?.generation.toString()], enabled: !!policy.data && delaySupported(), queryFn: async () => (await fetchSecurityDelays(client, fleet!.customerId)) ?? null });
  const queue = useQuery({ queryKey: ["organization-queue", fleet?.customerId, policy.data?.generation.toString(), timing.data], enabled: !!policy.data && timing.isSuccess, queryFn: () => fetchQueuedSecurityActions(client, fleet!.customerId, policy.data!.generation, policy.data!.requirements, timing.data ?? []), refetchInterval: 10000 });
  if (!fleet) return <section aria-label="Organization security" className={`${styles.workspace} space-y-5`}>
    <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg tracking-tight">Organization security</h2>
      <p className="mt-2 max-w-xl">Select an organization in the fleet selector to see its current protections and manage security across its fleets.</p>
      <p className="mt-3 text-xs text-slate-500">Security settings are shown only after the organization is selected and its policy can be verified.</p>
    </div>
    <details className={styles.setup}><summary>Advanced multi-signature tools</summary><div className={styles.setupBody}><MultisigAdministrator /></div></details>
  </section>;
  const refresh = async () => { await policy.refetch(); await timing.refetch(); await queue.refetch(); };
  const run = async (transaction: Parameters<typeof mutation.mutateAsync>[0]["transaction"]) => { try { setError(""); await mutation.mutateAsync({ transaction }); await refresh(); } catch (e) { setError(e instanceof Error ? e.message : "Security action failed"); } };
  const submit = async (edit: SecurityEdit) => {
    try {
      setError("");
      if (policy.data!.requirements.some(v => v !== 0)) {
        const bytes = await currentCustomerBytes(client, fleet.customerId);
        await run(buildQueueSecurityTx(fleet.customerId, editArguments(bytes, edit), BigInt(Date.now()) + BigInt(604800000)));
      } else await run(edit.operation === "set_security" ? buildSecurityPolicyTx(fleet.customerId, edit.values) : buildDelayConfigurationTx(fleet.customerId, edit.values));
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to prepare security action"); }
  };
  const configuredActions = policy.data?.requirements.filter(value => value !== 0).length;
  const openSetup = (id: string) => {
    const panel = document.getElementById(id);
    if (panel instanceof HTMLDetailsElement) { panel.open = true; panel.scrollIntoView({ behavior: "smooth", block: "start" }); }
  };
  const presentations: Record<number, { name: string; description: string; target?: string }> = {
    1: { name: "Multiple signers", description: "Require a chosen number of signatures from independently controlled keys.", target: "security-multisig" },
    2: { name: "Passkey verification", description: "Add a registered passkey alongside the administrator’s wallet approval." },
    4: { name: "Second-person approval", description: "Have another authorized administrator review sensitive changes.", target: "security-second-person" },
    8: { name: "Delay and cancellation", description: "Allow time to review and cancel a sensitive change before it takes effect.", target: "security-delays" },
  };
  return <section className={`${styles.workspace} space-y-6`} aria-label="Organization security">
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg tracking-tight">Organization security</h2><p className="mt-1">Manage protections for <strong className="font-medium text-slate-800">{fleet.customerName}</strong> and every fleet it owns.</p></div>
      <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-500">Organization-wide</span>
    </header>
    <div className={`rounded-2xl border p-5 ${policy.data ? configuredActions ? "border-blue-200 bg-blue-50/60" : "border-slate-200 bg-slate-50" : "border-amber-200 bg-amber-50/50"}`}>
      <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">Current protection</p>
      <h3 className="mt-2">{!securitySupported() ? "Security settings unavailable" : policy.isPending ? "Checking organization security…" : policy.error ? "Protection status could not be verified" : !policy.data ? "Security setup needs attention" : configuredActions ? `${configuredActions} of ${SECURITY_ACTIONS.length} actions have extra protection` : "No additional protections enabled"}</h3>
      <p className="mt-2 max-w-2xl">{!securitySupported() ? "This deployment needs an upgrade before these settings can be verified or managed here." : policy.isPending ? "Reading the saved policy. No security changes are being made." : policy.error ? "Refresh or check your connection before making changes. An unreadable policy does not mean protection is off." : !policy.data ? "This organization uses an older security setup. Migrate it before configuring these protections; its current protection status is not shown here." : "When an action has more than one protection, every requirement must pass. Changes still require the organization’s current authorization."}</p>
      {policy.data && <details className="mt-4"><summary className="text-xs">Verification details</summary><p className="mt-2 text-xs text-slate-500">Verified policy generation: {policy.data.generation.toString()}.</p></details>}
    </div>
    {[policy.error, timing.error, queue.error].map((e, i) => e && <div role="alert" key={i}><p>Some security information could not be loaded. Refresh before making changes.</p><details className="mt-2"><summary>Connection details</summary><p>{e.message}</p></details></div>)}
    <div className="grid gap-4 sm:grid-cols-2">
      {SECURITY_FACTORS.map(factor => {
        const view = presentations[factor.bit];
        const count = policy.data?.requirements.filter(value => (value & factor.bit) !== 0).length;
        const available = securitySupported() && (AVAILABLE_SECURITY_FACTORS & factor.bit) !== 0 && (factor.bit !== 8 || delaySupported());
        return <article key={factor.bit} className="flex min-w-0 flex-col rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-4 flex items-start justify-between gap-3"><span aria-hidden="true" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-700"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M12 3 4 6v6c0 4 8 9 8 9s8-5 8-9V6z"/><path d="m8 12 3 3 5-6"/></svg></span>
            <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${count ? "bg-blue-50 text-blue-800" : "bg-slate-100 text-slate-600"}`}>{count ? `Configured · ${count} ${count === 1 ? "action" : "actions"}` : !available ? "Unavailable" : policy.data ? "Not enabled" : "Not verified"}</span></div>
          <h3>{view.name}</h3><p className="mt-2 flex-1 text-sm">{view.description}</p>
          {available && view.target ? <button type="button" className="mt-5 self-start" onClick={() => openSetup(view.target!)}>View setup <span aria-hidden="true">→</span></button>
            : <p className="mt-4 text-xs text-slate-500">{factor.bit === 2 ? "Passkey setup is not available in this deployment." : "A compatible deployment is required to manage this protection."}</p>}
        </article>;
      })}
    </div>
    {policy.data && <details className={styles.setup}>
      <summary>Protection by action<span className="mt-1 block text-xs font-normal text-slate-500">See which requirements apply to each type of change</span></summary>
      <div className="divide-y divide-slate-100 px-5">{SECURITY_ACTIONS.map((name, i) => <div key={name} className="flex flex-wrap items-start justify-between gap-2 py-4"><span className="font-medium text-slate-800">{name}</span><span className="text-sm text-slate-500">{policy.data!.requirements[i] === 0 ? "No additional protection" : SECURITY_FACTORS.filter(f => (policy.data!.requirements[i] & f.bit) !== 0).map(f => presentations[f.bit].name).join(" + ")}</span></div>)}</div>
    </details>}
    <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-4">
      <h3>Keep a recovery path</h3><p className="mt-1">Losing required signers or credentials can permanently lock administration. Set up independent recovery access before enabling protections. There is no email or support reset.</p>
    </div>
    <section aria-label="Security changes awaiting review" className="space-y-4 rounded-2xl border border-amber-200 bg-amber-50/60 p-5">
      <h3>Security changes awaiting review</h3>
      <p>Any current administrator can cancel a queued action immediately. Pausing a deployment skips the delay; resuming uses the full policy. Execution uses the network clock; the countdown below uses your browser clock and is an estimate.</p>
      <ul className="space-y-3" aria-label="Queued security actions">{queue.data?.map(action => <li className="rounded-xl border border-slate-200 bg-slate-50 p-4" key={action.id}>
        <h3>{action.edit?.operation === "set_security" ? "Change security requirements" : action.edit ? "Change cancellation delays" : "Protected action"}</h3>
        <p className="mt-2">{delayStatus(action, now)}. Expires {new Date(Number(action.expires)).toLocaleString()}.</p>
        <details className="mt-3"><summary>Request details</summary><p>Request: {action.id}. Proposed by {action.proposer}.</p></details>
        {action.edit && <p>Requested changes: {action.edit.values.map((v, i) => `${SECURITY_ACTIONS[i]}: ${action.edit!.operation === "configure_security_delays" ? `${v / 1000} seconds` : v === 0 ? "Off" : SECURITY_FACTORS.filter(f => (v & f.bit) !== 0).map(f => presentations[f.bit].name).join(" + ")}`).join("; ")}.</p>}
        <div className="mt-3 flex flex-wrap gap-2">{fleet.administrator && <button disabled={mutation.isPending} onClick={() => run(buildCancelSecurityTx(fleet.customerId, action.id))}>Cancel action</button>}
        {fleet.administrator && action.edit && action.proposer === account?.address && <button disabled={mutation.isPending || action.stale || action.readyAt === undefined || now < action.readyAt || now >= action.expires} onClick={() => run(buildExecuteSecurityEditTx(fleet.customerId, action.id, action.edit!))}>Execute action</button>}</div>
        {!action.edit && <p>This action can be canceled here. Submit execution through the client that queued it.</p>}
      </li>)}</ul>
      {queue.isSuccess && queue.data.length === 0 && <p className="rounded-xl bg-slate-50 p-4">No queued actions found in available history.</p>}
      <SecondPersonApprovals />
    </section>
    <details id="security-delays" className={styles.setup}>
      <summary>Delay and cancellation settings<span className="mt-1 block text-xs font-normal text-slate-500">Configure review windows for sensitive changes</span></summary>
      <div className={`${styles.setupBody} space-y-5`}>
        {!delaySupported() && <p>Delay and cancellation settings are unavailable on this deployment.</p>}
        {policy.data && timing.isSuccess && fleet.administrator && <DelaySettings key={`${fleet.customerId}:${policy.data.generation}`} requirements={policy.data.requirements} delays={timing.data ?? undefined} busy={mutation.isPending} submit={submit}/>}
      </div>
    </details>
    <details id="security-second-person" className={styles.setup}>
      <summary>Second-person approval<span className="mt-1 block text-xs font-normal text-slate-500">Configure independent approval</span></summary>
      <div className={`${styles.setupBody} space-y-6`}>
        {(AVAILABLE_SECURITY_FACTORS & 4) === 0 && <p>Second-person approval settings are unavailable on this deployment.</p>}
        <SecondPersonSetup key={fleet.customerId} />
      </div>
    </details>
    <details id="security-multisig" className={styles.setup}>
      <summary>Advanced multi-signature tools<span className="mt-1 block text-xs font-normal text-slate-500">Manage signing members and collect transaction signatures</span></summary>
      <div className={styles.setupBody}><MultisigAdministrator key={fleet.customerId} customerId={fleet.customerId}/></div>
    </details>
    <details className="px-1 text-sm"><summary>How organization protections work</summary>
      <div className="mt-3 space-y-3"><p>Protected organizations keep publishers and fleet capabilities in organization custody. Generic capability loans are unavailable while protection is enabled. Transferring an organization requires recipient confirmation.</p>
      <p>Disabling protection or lowering delays requires the currently effective protections and cancellation window. A canceled or invalidated action must be queued again.</p></div>
    </details>
    {error && <p role="alert">{error}</p>}
  </section>;
}

function DelaySettings({ requirements, delays, busy, submit }: { requirements: number[]; delays?: number[]; busy: boolean; submit: (edit: SecurityEdit) => Promise<void> }) {
  const [masks, setMasks] = useState(requirements);
  const [seconds, setSeconds] = useState((delays ?? Array(7).fill(60000)).map(v => String(v / 1000)));
  const [error, setError] = useState("");
  const queued = requirements.some(v => v !== 0);
  return <fieldset disabled={busy} className="space-y-4"><legend className="mb-2">Review windows</legend>
    <p>Configure each class from 1 second to 6 days. Save delays before enabling protection. Settings changes wait for the longest currently enabled class. Queue entries expire after seven days.</p>
    {SECURITY_ACTIONS.map((name, i) => <div key={name} className="grid items-start gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4 sm:grid-cols-[minmax(0,1fr)_160px]"><label className="flex items-center gap-3"><input type="checkbox" checked={!!(masks[i] & 8)} disabled={!delays} onChange={e => setMasks(masks.map((v, j) => j === i ? e.target.checked ? v | 8 : v & 7 : v))}/> {name}</label> <label className="block text-xs">Delay in seconds <input className="block w-full" type="number" min="1" max="518400" step="1" value={seconds[i]} onChange={e => setSeconds(seconds.map((v, j) => j === i ? e.target.value : v))}/></label></div>)}
    <button onClick={() => { try { setError(""); const values = validateDelays(seconds.map(v => Number(v) * 1000)); void submit({ operation: "configure_security_delays", values }); } catch (e) { setError((e as Error).message); } }}>{queued ? "Queue delay changes" : "Save delays"}</button>
    <button disabled={!delays} onClick={() => submit({ operation: "set_security", values: masks })}>{queued ? "Queue protection changes" : "Save protection choices"}</button>
    {error && <p role="alert">{error}</p>}
  </fieldset>;
}
