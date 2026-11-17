import type { EntryView } from "./enrollment";

/**
 * Lost enrollment credential recovery (SF-277). The browser never keeps claim
 * keys, so a lost file cannot be downloaded again: an unused claim must be
 * revoked on chain before its serial is reserved and a new file issued.
 * Enrolled devices never need, and must never be given, regenerated identities.
 */
export interface LostCredentialPlan {
  /** Registered on chain and unused: revoke before reissuing. */
  revoke: EntryView[];
  /** Never registered, already revoked or withdrawn: remove the reservation, then reserve again. */
  release: EntryView[];
  /** Enrolled: the credential is spent and the device keeps its own key. */
  enrolled: EntryView[];
  /** Registration outcome unknown: refresh before acting. */
  wait: EntryView[];
  /** Conflicting or foreign records: do not act automatically. */
  blocked: EntryView[];
  /** Serials not in this job. */
  unknown: string[];
}

/** Serials match case-insensitively, as in the planner below. */
const serialKey = (serial: string) => serial.trim().toLowerCase();

/** Split pasted serials, keeping the first spelling of each case-insensitive serial. */
export function parseSerials(text: string): string[] {
  const seen = new Map<string, string>();
  for (const serial of text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean)) {
    if (!seen.has(serialKey(serial))) seen.set(serialKey(serial), serial);
  }
  return [...seen.values()];
}

export function planLostCredentials(views: EntryView[], serials: string[]): LostCredentialPlan {
  const plan: LostCredentialPlan = { revoke: [], release: [], enrolled: [], wait: [], blocked: [], unknown: [] };
  // Each claim appears at most once: a repeated revoke_claim aborts the whole
  // transaction, leaving every lost credential in it live.
  const planned = new Set<string>();
  const requested = new Set<string>();
  for (const serial of serials) {
    if (requested.has(serialKey(serial))) continue;
    requested.add(serialKey(serial));
    const matches = views.filter((view) => serialKey(view.entry.serial) === serialKey(serial));
    if (!matches.length) { plan.unknown.push(serial); continue; }
    for (const view of matches) {
      if (planned.has(view.entry.claim)) continue;
      planned.add(view.entry.claim);
      switch (view.status) {
        case "awaiting-device": case "unfunded": plan.revoke.push(view); break;
        case "unregistered": case "revoked": case "withdrawn": plan.release.push(view); break;
        case "binding": case "complete": plan.enrolled.push(view); break;
        case "confirming": plan.wait.push(view); break;
        default: plan.blocked.push(view);
      }
    }
  }
  return plan;
}
