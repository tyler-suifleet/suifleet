"use client";
import { policyChangeTx } from "@/lib/second-person";
import { PasskeySettings } from "./PasskeySettings";
import { passkeySupported } from "@/lib/passkey-factor";
import { SecondPersonSetup } from "./SecondPersonSetup";
import { SecondPersonApprovals } from "./SecondPersonApprovals";
import { MultisigAdministrator } from "./MultisigAdministrator";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useCurrentAccount, useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { SECURITY_ACTIONS, SECURITY_FACTORS, buildSecurityPolicyTx, fetchOrganizationSecurity, securitySupported } from "@/lib/security-policy";
import { buildCancelSecurityTx, buildDelayConfigurationTx, buildExecuteSecurityEditTx, buildQueueSecurityTx, currentCustomerBytes, delayStatus, delaySupported, editArguments, fetchQueuedSecurityActions, fetchSecurityDelays, validateDelays, type SecurityEdit } from "@/lib/security-delay";

export function OrganizationSecurity() {
  const selection = useFleetSelection(), client = useCurrentClient(), account = useCurrentAccount(), mutation = useFleetTransaction();
  const fleet = selection.fleet;
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => BigInt(Date.now()));
  useEffect(() => { const timer = setInterval(() => setNow(BigInt(Date.now())), 1000); return () => clearInterval(timer); }, []);
  const policy = useQuery({ queryKey: ["organization-security", fleet?.customerId], enabled: !!fleet && securitySupported(), queryFn: () => fetchOrganizationSecurity(client, fleet!.customerId), refetchInterval: 10000 });
  const timing = useQuery({ queryKey: ["organization-delays", fleet?.customerId, policy.data?.generation.toString()], enabled: !!policy.data && delaySupported(), queryFn: () => fetchSecurityDelays(client, fleet!.customerId) });
  const queue = useQuery({ queryKey: ["organization-queue", fleet?.customerId, policy.data?.generation.toString(), timing.data], enabled: !!policy.data && timing.isSuccess, queryFn: () => fetchQueuedSecurityActions(client, fleet!.customerId, policy.data!.generation, policy.data!.requirements, timing.data ?? []), refetchInterval: 10000 });
  if (!fleet) return <><p>Select an organization to view its security policy.</p><MultisigAdministrator/></>;
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
  return <section className="space-y-4" aria-label="Organization security">
    <p>These settings apply to every fleet in {fleet.customerName}. Enabled requirements combine: every selected protection must pass. All four default off.</p>
    {!securitySupported() && <p>The connected deployment does not include the security framework. This organization is unprotected.</p>}
    {policy.isPending && securitySupported() && <p>Reading the on-chain policy…</p>}
    {[policy.error, timing.error, queue.error].map((e, i) => e && <p role="alert" key={i}>{e.message}</p>)}
    {policy.isSuccess && !policy.data && <p>This organization has legacy authority. Migrate it before configuring protections. A missing policy does not mean protections are configured off.</p>}
    {policy.data && <><p>Verified policy generation: {policy.data.generation.toString()}.</p><ul>{SECURITY_ACTIONS.map((name, i) => <li key={name}>{name}: {policy.data!.requirements[i] === 0 ? "All protections off" : SECURITY_FACTORS.filter(f => (policy.data!.requirements[i] & f.bit) !== 0).map(f => f.name).join(" + ")}</li>)}</ul></>}
    <PasskeySettings />
    {policy.data && fleet.administrator && passkeySupported() && <button disabled={mutation.isPending} onClick={async () => { try { setError(""); const values = policy.data!.requirements.map(v => policy.data!.requirements.some(r => (r & 2) !== 0) ? v & ~2 : v | 2); await mutation.mutateAsync({ transaction: await policyChangeTx(client, fleet.customerId, values) }); await policy.refetch(); } catch (e) { setError(e instanceof Error ? e.message : "Passkey policy update failed"); } }}>Save or request passkey protection change for all actions</button>}
    {!delaySupported() && <p>Delay and cancellation require the upgraded package and its verified delay type origins.</p>}
    {policy.data && timing.isSuccess && fleet.administrator && <DelaySettings key={`${fleet.customerId}:${policy.data.generation}`} requirements={policy.data.requirements} delays={timing.data} busy={mutation.isPending} submit={submit}/>}
    <p>Any current administrator can cancel a queued action immediately. Pausing a deployment skips the delay; resuming uses the full policy. Execution uses the chain clock. The countdown uses your browser clock and is advisory.</p>
    <ul className="space-y-3" aria-label="Queued security actions">{queue.data?.map(action => <li className="rounded border p-3" key={action.id}>
      <p>{action.edit?.operation === "set_security" ? "Change security requirements" : action.edit ? "Change cancellation delays" : "Protected action"}: {action.id}</p>
      <p>Proposed by {action.proposer}. {delayStatus(action, now)}. Expires {new Date(Number(action.expires)).toLocaleString()}.</p>
      {action.edit && <p>Requested changes: {action.edit.values.map((v, i) => `${SECURITY_ACTIONS[i]}: ${action.edit!.operation === "configure_security_delays" ? `${v / 1000} seconds` : v === 0 ? "Off" : SECURITY_FACTORS.filter(f => (v & f.bit) !== 0).map(f => f.name).join(" + ")}`).join("; ")}.</p>}
      {fleet.administrator && <button disabled={mutation.isPending} onClick={() => run(buildCancelSecurityTx(fleet.customerId, action.id))}>Cancel action</button>}
      {fleet.administrator && action.edit && action.proposer === account?.address && <button disabled={mutation.isPending || action.stale || action.readyAt === undefined || now < action.readyAt || now >= action.expires} onClick={() => run(buildExecuteSecurityEditTx(fleet.customerId, action.id, action.edit!))}>Execute action</button>}
      {!action.edit && <p>This action can be canceled here. Submit execution through the client that queued it.</p>}
    </li>)}</ul>
    {queue.isSuccess && queue.data.length === 0 && <p>No queued actions found in available chain history.</p>}
    <p>Protected organizations keep publishers and fleet capabilities in organization custody. Generic capability loans are unavailable while protection is enabled. Whole-organization handover requires recipient confirmation.</p>
    <p>Disabling protection or lowering delays requires the currently effective protections and cancellation window. There is no email or support reset. A canceled or invalidated action must be queued again.</p>
    <SecondPersonSetup key={fleet.customerId} />
    <SecondPersonApprovals />
    <MultisigAdministrator key={fleet.customerId} customerId={fleet.customerId}/>
    {error && <p role="alert">{error}</p>}
  </section>;
}
function DelaySettings({ requirements, delays, busy, submit }: { requirements: number[]; delays?: number[]; busy: boolean; submit: (edit: SecurityEdit) => Promise<void> }) {
  const [masks, setMasks] = useState(requirements);
  const [seconds, setSeconds] = useState((delays ?? Array(7).fill(60000)).map(v => String(v / 1000)));
  const [error, setError] = useState("");
  const queued = requirements.some(v => v !== 0);
  return <fieldset disabled={busy} className="space-y-3"><legend>Delay and cancellation</legend>
    <p>Configure each class from 1 second to 6 days. Save delays before enabling protection. Settings changes wait for the longest currently enabled class. Queue entries expire after seven days.</p>
    {SECURITY_ACTIONS.map((name, i) => <div key={name}><label><input type="checkbox" checked={!!(masks[i] & 8)} disabled={!delays} onChange={e => setMasks(masks.map((v, j) => j === i ? e.target.checked ? v | 8 : v & 7 : v))}/> {name}</label> <label>Delay in seconds <input type="number" min="1" max="518400" step="1" value={seconds[i]} onChange={e => setSeconds(seconds.map((v, j) => j === i ? e.target.value : v))}/></label></div>)}
    <button onClick={() => { try { setError(""); const values = validateDelays(seconds.map(v => Number(v) * 1000)); void submit({ operation: "configure_security_delays", values }); } catch (e) { setError((e as Error).message); } }}>{queued ? "Queue delay changes" : "Save delays"}</button>
    <button disabled={!delays} onClick={() => submit({ operation: "set_security", values: masks })}>{queued ? "Queue protection changes" : "Save protection choices"}</button>
    {error && <p role="alert">{error}</p>}
  </fieldset>;
}
