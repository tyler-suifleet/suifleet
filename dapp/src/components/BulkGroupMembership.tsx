"use client";

// SF-171: review, then apply, bulk group membership changes in bounded
// transactions. A failure stops the run and reports each device's outcome;
// nothing is resubmitted without a fresh review.
import { useMemo, useState } from "react";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { existingInboxes, inboxSupported } from "@/lib/fleet-inbox";
import { buildMembershipTx, failureStatus, groupBatchSupported, membershipOutcome, planMembership, reconcileResults, MAX_FLEET_CHANGES, MAX_WALLET_CHANGES,
  type BatchResult, type DigestLookup, type MembershipAction, type MembershipChange, type MembershipOutcome, type MembershipPlan } from "@/lib/group-batch";
import type { NamedFleet } from "@/lib/customer-fleets";
import type { DeviceGroup, DeviceObject } from "@/lib/sui";

export function BulkGroupMembership({ devices, groups, selected, fleet, reloadGroups, onClose }: {
  devices: DeviceObject[]; groups: DeviceGroup[]; selected: string[]; fleet?: NamedFleet;
  /** Refresh membership after a run. */
  reloadGroups: () => Promise<unknown>;
  onClose: () => void;
}) {
  const client = useCurrentClient();
  const { mutateAsync } = useFleetTransaction();
  const [action, setAction] = useState<MembershipAction>("add");
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [includeMismatched, setIncludeMismatched] = useState(false);
  const [running, setRunning] = useState<{ plan: MembershipPlan; results: BatchResult[]; current?: number } | null>(null);
  const [outcome, setOutcome] = useState<MembershipOutcome | null>(null);
  const [checking, setChecking] = useState(false);
  const plan = useMemo(() => {
    try { return planMembership(action, selected, groupIds, devices, groups, { managed: !!fleet, includeMismatched }); }
    catch (err) { return err instanceof Error ? err : Error(String(err)); }
  }, [action, selected, groupIds, devices, groups, fleet, includeMismatched]);
  const name = (deviceId: string) => devices.find((d) => d.objectId === deviceId)?.name ?? `${deviceId.slice(0, 12)}…`;
  const groupName = (groupId: string) => groups.find((g) => g.objectId === groupId)?.name ?? `${groupId.slice(0, 12)}…`;

  // Effects of a submitted digest from the verified fleet node; a lookup
  // error (not found yet, node unavailable) leaves the result ambiguous.
  const lookup: DigestLookup = async (digest) => {
    const result = await client.getTransaction({ digest, include: { effects: true } });
    return result.Transaction?.status.success ? "succeeded" : "failed";
  };

  async function settle(frozen: MembershipPlan, results: BatchResult[]) {
    const reconciled = await reconcileResults(results, lookup);
    await reloadGroups().catch(() => undefined);
    setRunning({ plan: frozen, results: reconciled });
    setOutcome(membershipOutcome(frozen, reconciled));
  }

  async function apply(frozen: MembershipPlan) {
    const results: BatchResult[] = [];
    setRunning({ plan: frozen, results });
    for (const [index, changes] of frozen.transactions.entries()) {
      setRunning({ plan: frozen, results: [...results], current: index });
      let reachedWallet = false, definiteFailure = false, submitted: string | undefined;
      try {
        // Inbox existence is read per transaction: an earlier one may have created it.
        const inboxes = !fleet && inboxSupported() ? await existingInboxes(client, [...new Set(changes.map((c) => c.deviceId))]) : undefined;
        const { digest } = await mutateAsync({ transaction: buildMembershipTx(frozen.action, changes, devices, fleet, inboxes),
          onProgress: (phase) => { if (phase !== "validating") reachedWallet = true; },
          onSubmitted: (digest) => { submitted = digest; },
          onDefiniteFailure: () => { definiteFailure = true; } });
        results.push({ index, status: "succeeded", digest });
      } catch (err) {
        // A transport error or confirmation timeout may hide a transaction
        // that still executes: keep it ambiguous, with its digest if known.
        results.push({ index, status: failureStatus(reachedWallet, definiteFailure), digest: submitted, error: err instanceof Error ? err.message : String(err) });
        break;
      }
    }
    await settle(frozen, results);
  }

  async function recheck() {
    if (!running || checking) return;
    setChecking(true);
    try { await settle(running.plan, running.results); } finally { setChecking(false); }
  }

  const busy = running !== null && outcome === null;
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={busy ? undefined : onClose}>
      <div role="dialog" aria-label="Change group membership" className="bg-white rounded-lg shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto space-y-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold">Change groups for {selected.length} selected device(s)</h2>
          {!busy && <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">&times;</button>}
        </div>
        {!running && <>
          <fieldset className="flex gap-4 text-sm">
            <legend className="sr-only">Action</legend>
            {(["add", "remove"] as const).map((a) => (
              <label key={a} className="flex items-center gap-1">
                <input type="radio" name="bulk-action" checked={action === a} onChange={() => setAction(a)} /> {a === "add" ? "Add to groups" : "Remove from groups"}
              </label>
            ))}
          </fieldset>
          <fieldset className="text-sm space-y-1">
            <legend className="text-xs font-medium text-gray-500 uppercase mb-1">Groups</legend>
            {groups.length === 0 ? <p className="text-gray-400">No groups yet. Create one on the Device groups tab.</p> : groups.map((g) => (
              <label key={g.objectId} className="flex items-center gap-2">
                <input type="checkbox" checked={groupIds.includes(g.objectId)}
                  onChange={(e) => setGroupIds((ids) => e.target.checked ? [...ids, g.objectId] : ids.filter((x) => x !== g.objectId))} />
                {g.name} <span className="text-xs text-gray-400">{g.deviceIds.length} member(s)</span>
              </label>
            ))}
          </fieldset>
          {action === "add" && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={includeMismatched} onChange={(e) => setIncludeMismatched(e.target.checked)} />
              Also add devices whose architecture differs from the group&apos;s current members
            </label>
          )}
          {plan instanceof Error ? <p role="alert" className="text-sm text-red-600">{plan.message}</p> : groupIds.length > 0 && <Review plan={plan} name={name} groupName={groupName} managed={!!fleet} />}
          <div className="flex gap-2 pt-2">
            <button disabled={plan instanceof Error || !plan.changes.length} onClick={() => !(plan instanceof Error) && void apply(plan)}
              className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white px-4 py-2 rounded text-sm disabled:opacity-50">
              {plan instanceof Error || !plan.changes.length ? "Nothing to change" : `Approve ${plan.transactions.length} transaction(s)`}
            </button>
            <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Cancel</button>
          </div>
        </>}
        {running && !outcome && (
          <p className="text-sm" role="status">Transaction {(running.current ?? running.plan.transactions.length - 1) + 1} of {running.plan.transactions.length}: approve it in your wallet…</p>
        )}
        {running && outcome && <Outcome plan={running.plan} results={running.results} outcome={outcome} name={name} groupName={groupName} checking={checking} onRecheck={() => void recheck()} onClose={onClose} />}
      </div>
    </div>
  );
}

function Review({ plan, name, groupName, managed }: { plan: MembershipPlan; name: (id: string) => string; groupName: (id: string) => string; managed: boolean }) {
  return (
    <section aria-label="Review changes" className="text-sm space-y-2 border-t pt-3">
      <p className="font-medium">{plan.changes.length} change(s) in {plan.transactions.length} transaction(s): {plan.transactions.length} wallet approval(s),
        at most {managed ? MAX_FLEET_CHANGES : MAX_WALLET_CHANGES} changes each.</p>
      {managed && groupBatchSupported() && plan.action === "add" && <p className="text-xs text-gray-500">The chain re-checks each device&apos;s registered architecture against this review.</p>}
      <ChangeList title={plan.action === "add" ? "Add" : "Remove"} changes={plan.changes} name={name} groupName={groupName} />
      {plan.mismatches.length > 0 && (
        <div className="text-amber-800 bg-amber-50 border border-amber-200 rounded px-3 py-2">
          <p className="font-medium">Architecture differs from the group&apos;s members</p>
          <ul className="text-xs">{plan.mismatches.map((m) => <li key={`${m.groupId}-${m.deviceId}`}>{name(m.deviceId)} ({m.arch}) → {groupName(m.groupId)} ({m.groupArchs.join(", ")})</li>)}</ul>
        </div>
      )}
      {plan.skipped.length > 0 && (
        <details>
          <summary className="text-xs text-gray-500">{plan.skipped.length} skipped</summary>
          <ul className="text-xs text-gray-500">{plan.skipped.map((s) => <li key={`${s.groupId}-${s.deviceId}`}>{name(s.deviceId)} · {groupName(s.groupId)}: {s.reason}</li>)}</ul>
        </details>
      )}
    </section>
  );
}

function ChangeList({ title, changes, name, groupName }: { title: string; changes: MembershipChange[]; name: (id: string) => string; groupName: (id: string) => string }) {
  const byGroup = new Map<string, string[]>();
  for (const c of changes) byGroup.set(c.groupId, [...(byGroup.get(c.groupId) ?? []), c.deviceId]);
  return (
    <ul className="text-xs space-y-1">
      {[...byGroup].map(([groupId, ids]) => <li key={groupId}><span className="font-medium">{title} {groupName(groupId)} ({ids.length}):</span> {ids.map(name).join(", ")}</li>)}
    </ul>
  );
}

function Outcome({ plan, results, outcome, name, groupName, checking, onRecheck, onClose }: { plan: MembershipPlan; results: BatchResult[]; outcome: MembershipOutcome;
  name: (id: string) => string; groupName: (id: string) => string; checking: boolean; onRecheck: () => void; onClose: () => void }) {
  const failure = results.find((r) => r.status !== "succeeded");
  const pending = results.filter((r) => r.status === "ambiguous");
  const verb = plan.action === "add" ? "Added" : "Removed";
  return (
    <section aria-label="Result" className="text-sm space-y-2">
      <p role={failure ? "alert" : "status"} className={failure ? "text-red-700" : "text-green-700"}>
        {failure ? `Transaction ${failure.index + 1} of ${plan.transactions.length} ${failure.status === "failed" ? "failed" : "was not confirmed"}: ${failure.error}. Later transactions were not sent.`
          : `All ${plan.transactions.length} transaction(s) succeeded.`}
      </p>
      {outcome.changed.length > 0 && <ChangeList title={verb} changes={outcome.changed} name={name} groupName={groupName} />}
      {outcome.unchanged.length > 0 && <div><p className="font-medium">Not changed ({outcome.unchanged.length})</p><ChangeList title="Unchanged" changes={outcome.unchanged} name={name} groupName={groupName} /></div>}
      {outcome.unknown.length > 0 && <div className="text-amber-800">
        <p className="font-medium">Unknown ({outcome.unknown.length}): the transaction may still execute. {pending.some((r) => r.digest)
          ? "Check its digest again before reviewing these changes."
          : "The wallet returned no digest; check its activity before reviewing these changes."}</p>
        <ChangeList title="Unknown" changes={outcome.unknown} name={name} groupName={groupName} />
        {pending.some((r) => r.digest) && <button disabled={checking} onClick={onRecheck} className="mt-1 px-3 py-1 text-xs bg-amber-100 rounded hover:bg-amber-200 disabled:opacity-50">{checking ? "Checking…" : "Check again"}</button>}
      </div>}
      {outcome.notAttempted.length > 0 && <div><p className="font-medium">Not attempted ({outcome.notAttempted.length})</p><ChangeList title="Not sent" changes={outcome.notAttempted} name={name} groupName={groupName} /></div>}
      <ul className="text-xs font-mono text-gray-500">{results.filter((r) => r.digest).map((r) => <li key={r.index}>Transaction {r.index + 1}: {r.digest}{r.status === "ambiguous" ? " (unconfirmed)" : r.status === "failed" ? " (failed)" : ""}</li>)}</ul>
      {failure && <p className="text-xs text-gray-500">Nothing is retried automatically. {pending.length ? "Resolve the unknown transaction first: reviewing again before it settles could submit the same changes twice." : "Review again to plan only the remaining changes."}</p>}
      <button onClick={onClose} className="px-4 py-2 text-sm bg-gray-100 rounded hover:bg-gray-200">Close</button>
    </section>
  );
}
