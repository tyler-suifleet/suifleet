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

export function parseSerials(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((serial) => serial.trim()).filter(Boolean))];
}

export function planLostCredentials(views: EntryView[], serials: string[]): LostCredentialPlan {
  const plan: LostCredentialPlan = { revoke: [], release: [], enrolled: [], wait: [], blocked: [], unknown: [] };
  const key = (serial: string) => serial.trim().toLowerCase();
  for (const serial of serials) {
    const matches = views.filter((view) => key(view.entry.serial) === key(serial));
    if (!matches.length) { plan.unknown.push(serial); continue; }
    for (const view of matches) {
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
