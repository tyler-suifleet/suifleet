import { decisionMessage } from '@/lib/rollout-form';
import type { RolloutDecisionView } from '@/lib/rollout-progress';

export function DecisionAudit({ history, revision, names }: { history: { decisions: RolloutDecisionView[]; complete: boolean; missing: string[]; pruned: boolean }; revision: bigint; names?: Map<string, string> }) {
  return <>
    <ul>{history.decisions.map(d => <li key={d.revision.toString()} className="text-xs">
      #{d.revision.toString()} · {new Date(Number(d.atMs)).toISOString()} · {d.state} · cohort {(d.cohort + BigInt(1)).toString()} · {decisionMessage(d)}
      {d.admitted.map(id => <span key={id}> · {names?.get(id) ?? 'Unavailable device'}<details><summary>Object ID</summary>{id}</details></span>)}
    </li>)}</ul>
    <p className="text-xs">{history.complete ? `Decision history complete: revisions 1–${revision}.` : <>
      Unreadable decision revisions: {history.missing.join(', ')}. {history.pruned ? 'This connection has pruned legacy events.' : 'These revisions have no stored audit entry and were not recovered from retained events.'}
    </>} <a href="/settings/#connection-history">Connection history explanation</a></p>
  </>;
}
