import type { ComponentVersion } from './catalog';
import { validateRollout, type RolloutInput } from './rollout';
import type { RolloutDecisionView } from './rollout-progress';

export type DurationUnit = 'seconds' | 'minutes' | 'hours';
export function durationMs(value: number, unit: DurationUnit): number {
  const ms = value * { seconds: 1000, minutes: 60000, hours: 3600000 }[unit];
  if (!Number.isSafeInteger(ms) || ms < 0 || ms > 604800000) throw Error('Durations must be between zero and seven days, in whole milliseconds');
  return ms;
}
export function policyInput(input: RolloutInput, durations: { rate: [number, DurationUnit]; bake: [number, DurationUnit]; stall: [number, DurationUnit] }, percent: number): RolloutInput {
  const failureBps = Math.round(percent * 100);
  if (!Number.isFinite(percent) || Math.abs(percent * 100 - failureBps) > 0.000001) throw Error('Failure percentage supports at most two decimal places');
  const result = { ...input, failureBps, rateWindowMs: durationMs(...durations.rate), bakeMs: durationMs(...durations.bake), offlineTimeoutMs: durationMs(...durations.stall) };
  validateRollout(result); return result;
}
export function libraryRecipe(version: ComponentVersion, index: number) {
  const m = version.manifests[index];
  if (!m) throw Error('Select a published platform recipe');
  return { component: version.name, version: version.version, recipe: m.recipeBlobId, artifactSha256: m.artifactSha256, recipeSha256: m.recipeSha256 };
}
/** Use confirmed audit reasons; never infer health or admission from a digest. */
export function decisionMessage(d: RolloutDecisionView, candidateCount?: number): string {
  const admitted = d.admitted.length;
  const next = d.state === 'Aborted' ? 'No more devices can be admitted. Create a new rollout after resolving the failures.' :
    d.state === 'Complete' ? 'All cohorts are complete.' : d.state === 'Paused' ? 'Review the audit reason and resolve any blocking condition before explicitly resuming.' :
    d.reason === 'deployment paused, cancelled or expired; no new grants' ? 'Review deployment control: resume a paused deployment or create a replacement for a cancelled or expired deployment before requesting grants.' :
    d.reason === 'created; no members admitted' ? 'Select waiting devices in the first cohort and return here to approve admission.' :
    d.reason === 'bake and health gate passed; next cohort' ? 'Select candidates in the next cohort and return here to approve admission.' :
    d.reason === 'baking completed cohort' ? 'Wait for the bake period, then return here to evaluate fresh health.' :
    admitted ? 'Wait for device receipts and verified health, then return here to approve the next step.' :
    candidateCount === 0 ? 'No admission candidates were selected. Select waiting devices to admit them, or wait for receipts, fresh health or rate capacity before evaluating again.' :
    'Check receipts, fresh health, concurrency and rate capacity, then return here to approve the next step.';
  return `${admitted} device${admitted === 1 ? '' : 's'} admitted. ${d.reason}. ${next}`;
}
