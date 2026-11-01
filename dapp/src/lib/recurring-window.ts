// SF-174 recurring weekly maintenance windows. Devices evaluate them with the
// tz database pinned into the daemon; this browser preview uses Intl zone data
// and is advisory (it may differ around very recent tzdb changes).

export interface WeeklySlot { days: number; start_minute: number; duration_minutes: number }
export interface RecurringWindow { revision: string; timezone: string; slots: WeeklySlot[] }
export interface AdmissionWindow { start: number; end: number }

/** Bit 0 is Monday, matching the Move layout. */
export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
export const MAX_SLOTS = 8;
/** Reserved for a later per-device zone; current daemons defer on it. */
export const DEVICE_LOCAL = "device-local";
const MINUTE = 60_000;
const DAY = 86_400_000;

export function validateRecurring(r: Pick<RecurringWindow, "timezone" | "slots">) {
  if (!/^[A-Za-z0-9_+-][A-Za-z0-9/_+-]{0,63}$/.test(r.timezone)) throw new Error("Timezone must be an IANA name such as Europe/Berlin");
  if (r.timezone === DEVICE_LOCAL) throw new Error("Device-local timezones are not supported yet");
  if (!zoneSupported(r.timezone)) throw new Error(`Unknown timezone ${r.timezone}`);
  if (r.slots.length < 1 || r.slots.length > MAX_SLOTS) throw new Error(`Use 1 to ${MAX_SLOTS} weekly slots`);
  for (const s of r.slots) {
    if (!Number.isInteger(s.days) || s.days < 1 || s.days > 127) throw new Error("Each slot needs at least one weekday");
    if (!Number.isInteger(s.start_minute) || s.start_minute < 0 || s.start_minute > 1439) throw new Error("Slot start must be a time of day");
    if (!Number.isInteger(s.duration_minutes) || s.duration_minutes < 1 || s.duration_minutes > 1440) throw new Error("Slot duration must be 1 minute to 24 hours");
  }
}

export function zoneSupported(timezone: string): boolean {
  try { new Intl.DateTimeFormat("en-US", { timeZone: timezone }); return true; } catch { return false; }
}
export function browserZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}
export function knownZones(): string[] {
  const intl = Intl as typeof Intl & { supportedValuesOf?: (key: string) => string[] };
  return intl.supportedValuesOf ? intl.supportedValuesOf("timeZone") : [];
}

const formatters = new Map<string, Intl.DateTimeFormat>();
function wall(ms: number, timezone: string) {
  let f = formatters.get(timezone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: timezone, hourCycle: "h23", year: "numeric", month: "numeric",
      day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" });
    formatters.set(timezone, f);
  }
  const p = Object.fromEntries(f.formatToParts(new Date(ms)).map(x => [x.type, x.value]));
  return { y: Number(p.year), m: Number(p.month), d: Number(p.day), h: Number(p.hour) % 24, mi: Number(p.minute), s: Number(p.second) };
}
/** UTC offset in ms at an instant. */
function offset(ms: number, timezone: string): number {
  const w = wall(ms, timezone);
  const floored = ms - (((ms % 1000) + 1000) % 1000);
  return Date.UTC(w.y, w.m - 1, w.d, w.h, w.mi, w.s) - floored;
}
/** The UTC instant a local date/minute opens: in a DST gap the first valid
 * instant after it, in an overlap the earlier occurrence (as the daemon). */
export function opening(timezone: string, y: number, m: number, d: number, minute: number): number {
  const local = Date.UTC(y, m - 1, d, Math.floor(minute / 60), minute % 60);
  const before = offset(local - DAY, timezone), after = offset(local + DAY, timezone);
  const valid = [local - before, local - after].filter(t => offset(t, timezone) === local - t);
  if (valid.length) return Math.min(...valid);
  // Gap: binary-search the transition between the two candidate instants.
  let lo = Math.min(local - before, local - after), hi = Math.max(local - before, local - after);
  const initial = offset(lo, timezone);
  while (hi - lo > 1000) {
    const mid = lo + Math.floor((hi - lo) / 2000) * 1000;
    if (offset(mid, timezone) === initial) lo = mid; else hi = mid;
  }
  return hi;
}

/** Merged occurrences `[start, end)` that close after `from`, in order. */
export function occurrences(r: Pick<RecurringWindow, "timezone" | "slots">, from: number, days: number): AdmissionWindow[] {
  const today = wall(from, r.timezone);
  const spans: AdmissionWindow[] = [];
  for (let i = -2; i < days; i++) {
    const date = new Date(Date.UTC(today.y, today.m - 1, today.d + i));
    const bit = 1 << ((date.getUTCDay() + 6) % 7);
    for (const slot of r.slots.filter(s => s.days & bit)) {
      const start = opening(r.timezone, date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDate(), slot.start_minute);
      spans.push({ start, end: start + slot.duration_minutes * MINUTE });
    }
  }
  spans.sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: AdmissionWindow[] = [];
  for (const s of spans) {
    const last = merged[merged.length - 1];
    if (last && s.start <= last.end) last.end = Math.max(last.end, s.end); else merged.push({ ...s });
  }
  return merged.filter(w => w.end > from);
}

/** Schedule limits shared with the deployment Control (UTC ms; 0 is unset). */
export interface Limits { not_before_ms: string; expires_ms: string; window_start_ms: string; window_end_ms: string; state: number }
/** The next `count` admission windows, combining the recurring rule with
 * not-before, expiry and the absolute window. Notice is per device. */
export function nextAdmissionWindows(r: Pick<RecurringWindow, "timezone" | "slots"> | null, c: Limits, now: number, count = 3): AdmissionWindow[] {
  if (c.state === 2) return [];
  let lower = Math.max(now, Number(c.not_before_ms));
  let upper = Number(c.expires_ms) || Infinity;
  if (Number(c.window_end_ms)) { lower = Math.max(lower, Number(c.window_start_ms)); upper = Math.min(upper, Number(c.window_end_ms)); }
  if (lower >= upper) return [];
  if (!r) return [{ start: lower, end: upper }];
  const result: AdmissionWindow[] = [];
  // At least one opening per week; look ahead far enough for `count` of them.
  for (const w of occurrences(r, lower, 7 * count + 8)) {
    const start = Math.max(w.start, lower), end = Math.min(w.end, upper);
    if (start < end) result.push({ start, end });
    if (result.length === count || w.end >= upper) break;
  }
  return result;
}

export function recurringActive(r: Pick<RecurringWindow, "timezone" | "slots">, now: number): boolean {
  const [first] = occurrences(r, now, 9);
  return !!first && first.start <= now;
}

export function describeSlot(s: WeeklySlot): string {
  const days = WEEKDAYS.filter((_, i) => s.days & (1 << i)).join(", ");
  const h = Math.floor(s.duration_minutes / 60), m = s.duration_minutes % 60;
  return `${days} ${minuteLabel(s.start_minute)} for ${h ? `${h} h` : ""}${h && m ? " " : ""}${m ? `${m} min` : ""}`;
}
export function minuteLabel(minute: number): string {
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}
export function formatIn(ms: number, timezone: string): string {
  return new Intl.DateTimeFormat("en-GB", { timeZone: timezone, weekday: "short", year: "numeric", month: "short",
    day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" }).format(new Date(ms));
}
