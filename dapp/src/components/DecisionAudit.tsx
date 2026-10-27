import type { RolloutDecisionView } from '@/lib/rollout-progress';

export function DecisionAudit({ history, revision }: { history: { decisions: RolloutDecisionView[]; complete: boolean; missing: string[]; pruned: boolean }; revision: bigint }) {
  return <>
    <ul>{history.decisions.map(d => <li key={d.revision.toString()} className="text-xs">
      #{d.revision.toString()} · {new Date(Number(d.atMs)).toISOString()} · {d.state} · cohort {(d.cohort + BigInt(1)).toString()} · {d.reason}
      {d.admitted.length ? ` · admitted IDs: ${d.admitted.join(', ')}` : ''}
    </li>)}</ul>
    <p className="text-xs">{history.complete ? `Decision history complete: revisions 1–${revision}.` : <>
      Unreadable decision revisions: {history.missing.join(', ')}. {history.pruned ? 'This connection has pruned legacy events.' : 'These revisions have no stored audit entry and were not recovered from retained events.'}
    </>} <a href="/settings/#connection-history">Connection history explanation</a></p>
  </>;
}
