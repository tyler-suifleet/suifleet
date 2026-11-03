import { reportDecisionHistory } from "./event-history";
// Canary rollout progress (SF-084) derived only from the on-chain Rollout
// record and its RolloutDecision audit events (SF-017). A record that violates
// the contract's invariants is refused rather than partially displayed.
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { bcs } from "@mysten/sui/bcs";
import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { deployment_manager_RolloutDecisionBcs, deployment_manager_DecisionRecordBcs } from "./fleet-protocol";
import { moveType, requireManifest } from "./deployment-manifest";
import type { Rollout } from "./rollout";

export const ROLLOUT_STATES = ["Active", "Paused", "Aborted", "Complete"] as const;
export type MemberStatus = "waiting" | "granted" | "applied" | "failed" | "skipped";

export interface StageProgress {
  cohort: number;
  status: "done" | "current" | "pending";
  members: number;
  /** Members admitted (granted permission to apply), whatever their outcome. */
  granted: number;
  inflight: number;
  applied: number;
  failed: number;
  skipped: number;
  waiting: number;
}

export interface RolloutProgress {
  state: typeof ROLLOUT_STATES[number];
  aborted: boolean;
  revision: bigint;
  currentCohort: number;
  stages: StageProgress[];
  members: { device: string; cohort: number; status: MemberStatus; grantedAtMs: bigint | null }[];
  /** Settled outcomes over all cohorts, as the contract counts them. */
  samples: number;
  failures: number;
  policy: { concurrency: bigint; rateCount: bigint; rateWindowMs: bigint; bakeMs: bigint; minimumSamples: bigint; failureBps: bigint; offlineTimeoutMs: bigint };
  /** Whether the contract's abort rule is met by the recorded outcomes. */
  thresholdReached: boolean;
  /** Bake of the current cohort, when started. */
  bake: { startedMs: bigint; remainingMs: bigint } | null;
  progressMs: bigint;
}

const u64 = (value: string, name: string) => {
  if (!/^\d+$/.test(value)) throw new Error(`Rollout ${name} is not an unsigned integer`);
  const n = BigInt(value);
  if (n >= BigInt(2) ** BigInt(64)) throw new Error(`Rollout ${name} exceeds u64`);
  return n;
};

/** Strictly check the record against new_rollout_verified/advance/control invariants. */
export function rolloutProgress(r: Rollout, nowMs: number): RolloutProgress {
  const n = r.members.length;
  if (n < 1 || n > 512 || r.cohorts.length !== n || r.admitted_at.length !== n || r.outcomes.length !== n || r.skipped.length !== n) {
    throw new Error("Rollout member vectors are inconsistent");
  }
  const p = r.policy;
  const policy = { concurrency: u64(p.concurrency, "concurrency"), rateCount: u64(p.rate_count, "rate count"),
    rateWindowMs: u64(p.rate_window_ms, "rate window"), bakeMs: u64(p.bake_ms, "bake"), minimumSamples: u64(p.minimum_samples, "minimum samples"),
    failureBps: u64(p.failure_bps, "failure threshold"), offlineTimeoutMs: u64(p.offline_timeout_ms, "offline timeout") };
  const week = BigInt(604800000);
  if (policy.concurrency < BigInt(1) || policy.concurrency > BigInt(512) || policy.rateCount < BigInt(1) || policy.rateCount > BigInt(512)
    || policy.rateWindowMs < BigInt(1) || policy.rateWindowMs > week || policy.bakeMs > week || policy.minimumSamples < BigInt(1)
    || policy.minimumSamples > BigInt(512) || policy.failureBps < BigInt(1) || policy.failureBps > BigInt(10000)
    || policy.offlineTimeoutMs < BigInt(1) || policy.offlineTimeoutMs > week) throw new Error("Rollout policy is outside contract bounds");
  if (!Number.isInteger(r.state) || r.state < 0 || r.state > 3) throw new Error("Unknown rollout state");
  const cohorts = r.cohorts.map((c) => Number(u64(c, "cohort")));
  if (cohorts[0] !== 0 || cohorts.some((c, i) => i > 0 && c !== cohorts[i - 1] && c !== cohorts[i - 1] + 1)) {
    throw new Error("Rollout cohorts are not ordered");
  }
  const last = cohorts[n - 1], current = Number(u64(r.cohort, "current cohort"));
  if (current > last) throw new Error("Rollout current cohort does not exist");
  const devices = r.members.map((m) => normalizeSuiAddress(m));
  if (new Set(devices).size !== n) throw new Error("Rollout has a duplicate member");
  const members = devices.map((device, i) => {
    const granted = u64(r.admitted_at[i], "grant time"), outcome = r.outcomes[i], skipped = r.skipped[i];
    if (![0, 1, 2].includes(outcome)) throw new Error("Unknown rollout outcome");
    if (outcome !== 0 && granted === BigInt(0)) throw new Error("Rollout outcome without a grant");
    if (skipped && (granted !== BigInt(0) || outcome !== 0)) throw new Error("Skipped rollout member was admitted");
    if (granted !== BigInt(0) && cohorts[i] > current) throw new Error("Rollout granted a member of a later cohort");
    const status: MemberStatus = skipped ? "skipped" : outcome === 1 ? "applied" : outcome === 2 ? "failed" : granted ? "granted" : "waiting";
    return { device, cohort: cohorts[i], status, grantedAtMs: granted ? granted : null };
  });
  const stages: StageProgress[] = [];
  for (let cohort = 0; cohort <= last; cohort++) {
    const of = members.filter((m) => m.cohort === cohort);
    const count = (s: MemberStatus) => of.filter((m) => m.status === s).length;
    if (of.length < policy.minimumSamples) throw new Error("Rollout cohort is smaller than its minimum sample");
    stages.push({ cohort, status: r.state === 3 || cohort < current ? "done" : cohort === current ? "current" : "pending",
      members: of.length, granted: of.filter((m) => m.grantedAtMs !== null).length, inflight: count("granted"),
      applied: count("applied"), failed: count("failed"), skipped: count("skipped"), waiting: count("waiting") });
  }
  const samples = members.filter((m) => m.status === "applied" || m.status === "failed").length;
  const failures = members.filter((m) => m.status === "failed").length;
  const thresholdReached = BigInt(samples) >= policy.minimumSamples && BigInt(failures) * BigInt(10000) >= BigInt(samples) * policy.failureBps;
  const bakeStarted = u64(r.bake_started_ms, "bake start");
  const now = BigInt(Math.max(0, Math.floor(nowMs)));
  const elapsed = now > bakeStarted ? now - bakeStarted : BigInt(0);
  return {
    state: ROLLOUT_STATES[r.state], aborted: r.state === 2, revision: u64(r.revision, "revision"), currentCohort: current,
    stages, members, samples, failures, policy, thresholdReached,
    bake: r.state === 0 && bakeStarted > BigInt(0) ? { startedMs: bakeStarted, remainingMs: elapsed >= policy.bakeMs ? BigInt(0) : policy.bakeMs - elapsed } : null,
    progressMs: u64(r.progress_ms, "progress time"),
  };
}

/** Basis points as a percentage label (100 bps = 1%). */
export function bpsLabel(bps: bigint): string {
  const whole = bps / BigInt(100), part = bps % BigInt(100);
  return `${whole}${part ? `.${part.toString().padStart(2, "0").replace(/0$/, "")}` : ""}%`;
}

export interface RolloutDecisionView {
  revision: bigint; cohort: bigint; state: typeof ROLLOUT_STATES[number]; reason: string; atMs: bigint; admitted: string[];
}

/** Audited decisions of one rollout, newest first, checked against the record. */
export function rolloutDecisions(events: { bcs: Uint8Array }[], deploymentId: string, revision: bigint, limit = Number.MAX_SAFE_INTEGER): RolloutDecisionView[] {
  const id = normalizeSuiAddress(deploymentId);
  const seen = new Set<bigint>();
  const decisions: RolloutDecisionView[] = [];
  for (const event of events) {
    const d = deployment_manager_RolloutDecisionBcs.parse(event.bcs);
    if (normalizeSuiAddress(d.deployment_id) !== id) continue;
    const rev = u64(d.revision, "decision revision");
    if (rev < BigInt(1) || rev > revision || seen.has(rev) || d.state > 3) throw new Error("Rollout decision history is inconsistent");
    seen.add(rev);
    decisions.push({ revision: rev, cohort: u64(d.cohort, "decision cohort"), state: ROLLOUT_STATES[d.state], reason: d.reason,
      atMs: u64(d.at_ms, "decision time"), admitted: d.admitted.map((a) => normalizeSuiAddress(a)) });
  }
  decisions.sort((a, b) => (a.revision < b.revision ? 1 : a.revision > b.revision ? -1 : 0));
  return decisions.slice(0, limit);
}

/** Exact missing revision ranges, without allocating one item per missing revision. */
export function missingDecisionRevisions(decisions: RolloutDecisionView[], revision: bigint): string[] {
  const ascending = [...decisions].sort((a, b) => a.revision < b.revision ? -1 : 1);
  const gaps: string[] = [];
  let next = BigInt(1);
  for (const d of ascending) {
    if (d.revision > next) gaps.push(next === d.revision - BigInt(1) ? `${next}` : `${next}–${d.revision - BigInt(1)}`);
    next = d.revision + BigInt(1);
  }
  if (next <= revision) gaps.push(next === revision ? `${next}` : `${next}–${revision}`);
  return gaps;
}

/** Current object state is the audit source. Events only recover legacy gaps. */
export async function fetchRolloutDecisions(client: Pick<SuiGrpcClient, "listEvents" | "listDynamicFields">, deploymentId: string, revision: bigint, maxPages = 20) {
  const events: { bcs: Uint8Array }[] = [];
  const origins = requireManifest().type_origins;
  if (origins["deployment_manager::DecisionKey"] && origins["deployment_manager::DecisionRecord"]) {
    let cursor: string | null | undefined;
    const cursors = new Set<string>();
    do {
      const page = await client.listDynamicFields({ parentId: deploymentId, cursor, limit: 100, include: { value: true } });
      for (const field of page.dynamicFields) {
        if (normalizeStructTag(field.name.type) !== normalizeStructTag(moveType("deployment_manager::DecisionKey"))) continue;
        if (field.$kind !== "DynamicField" || normalizeStructTag(field.valueType) !== normalizeStructTag(moveType("deployment_manager::DecisionRecord"))) throw Error("Unexpected decision record type");
        const d = deployment_manager_DecisionRecordBcs.parse(field.value.bcs);
        if (bcs.u64().parse(field.name.bcs) !== d.revision || normalizeSuiAddress(d.deployment_id) !== normalizeSuiAddress(deploymentId)) throw Error("Decision record binding mismatch");
        // Identical layouts, separate abilities and type origins.
        events.push({ bcs: field.value.bcs });
      }
      if (!page.hasNextPage) break;
      if (!page.cursor || cursors.has(page.cursor)) throw Error("Decision pagination made no progress");
      cursors.add(page.cursor); cursor = page.cursor;
    } while (true);
  }
  let decisions = rolloutDecisions(events, deploymentId, revision);
  let missing = missingDecisionRevisions(decisions, revision);
  let pruned = false;
  if (missing.length) {
    const known = new Set(decisions.map(d => d.revision.toString()));
    const type = moveType("deployment_manager::RolloutDecision");
    let before: string | undefined;
    const cursors = new Set<string>();
    try {
      for (let i = 0; i < maxPages; i++) {
        const page = await client.listEvents({ filter: { eventType: type }, order: "descending", ...(before ? { before } : {}), limit: 50 });
        const recovered = rolloutDecisions(page.events, deploymentId, revision);
        for (const d of recovered) if (!known.has(d.revision.toString())) { decisions.push(d); known.add(d.revision.toString()); }
        missing = missingDecisionRevisions(decisions, revision);
        if (!missing.length || !page.hasNextPage) break;
        if (!page.endCursor || cursors.has(page.endCursor)) throw Error("Event pagination made no progress");
        cursors.add(page.endCursor); before = page.endCursor;
      }
    } catch (error) {
      if (!(error instanceof Error) || !/requested data below earliest available/i.test(error.message)) throw error;
      pruned = true;
    }
  }
  decisions.sort((a, b) => a.revision < b.revision ? 1 : -1);
  reportDecisionHistory(`${requireManifest().chain_identifier}:${deploymentId}`, missing.length > 0, pruned);
  return { decisions, complete: missing.length === 0, missing, pruned };
}
