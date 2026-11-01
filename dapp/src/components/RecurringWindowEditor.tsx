"use client";

import { useState } from "react";
import type { DeploymentControl } from "@/lib/deployment-control";
import {
  MAX_SLOTS, WEEKDAYS, browserZone, describeSlot, formatIn, knownZones, minuteLabel, nextAdmissionWindows,
  validateRecurring, type RecurringWindow, type WeeklySlot,
} from "@/lib/recurring-window";

type Draft = { timezone: string; slots: WeeklySlot[] };

/** SF-174 weekly maintenance windows: summary, next three admissions, editor. */
export function RecurringEditor({ control, recurring, supported, canEdit, pending, onSave }: {
  control: DeploymentControl; recurring: RecurringWindow | null; supported: boolean; canEdit: boolean; pending: boolean;
  onSave: (r: Draft | null) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => recurring
    ? { timezone: recurring.timezone, slots: recurring.slots.map(s => ({ ...s })) }
    : { timezone: browserZone(), slots: [{ days: 64, start_minute: 120, duration_minutes: 120 }] });
  const send = (r: Draft | null) => {
    try { onSave(r); } catch (error) { alert(error instanceof Error ? error.message : String(error)); }
  };
  const setSlot = (index: number, slot: Partial<WeeklySlot>) =>
    setDraft({ ...draft, slots: draft.slots.map((s, i) => i === index ? { ...s, ...slot } : s) });
  let draftError = "";
  try { validateRecurring(draft); } catch (error) { draftError = error instanceof Error ? error.message : String(error); }
  const shown = editing ? (draftError ? null : draft) : recurring;
  const timezone = shown?.timezone ?? "UTC";
  let upcoming: { start: number; end: number }[] = [];
  try { upcoming = shown ? nextAdmissionWindows(shown, control, Date.now()) : []; } catch { upcoming = []; }
  if (!supported && !recurring) return <p>Recurring weekly windows need the upgraded package; regenerate the manifest after it is published.</p>;
  return <div className="space-y-1" aria-label="Recurring maintenance window">
    <p>Recurring window: {recurring
      ? <>{recurring.slots.map(describeSlot).join("; ")} ({recurring.timezone})</>
      : "none (admission is not limited to weekly slots)"}</p>
    <p>Devices evaluate the window in its IANA zone with the tz database built into the daemon and record
      “outside recurring maintenance window” while they wait. This preview uses your browser’s zone data.</p>
    {(recurring || editing) && <div>
      <p>Next admission windows{editing ? " (draft)" : ""}, after not-before/expiry; device advance notice may delay each:</p>
      {upcoming.length ? <ol className="list-decimal ml-5" data-testid="next-windows">
        {upcoming.map(w => <li key={w.start}>
          {formatIn(w.start, timezone)} – {formatIn(w.end, timezone)} · UTC {new Date(w.start).toISOString()} – {Number.isFinite(w.end) ? new Date(w.end).toISOString() : "no end"}
        </li>)}
      </ol> : <p>None: the schedule leaves no admission window.</p>}
    </div>}
    {canEdit && supported && <div className="flex gap-3">
      <button disabled={pending} onClick={() => setEditing(!editing)}>{recurring ? "Edit recurring window" : "Add recurring window"}</button>
      {recurring && <button disabled={pending} onClick={() => { if (confirm("Remove the recurring window? Devices restart advance notice.")) send(null); }}>Remove recurring window</button>}
    </div>}
    {editing && <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); send(draft); }}>
      <label>Timezone (IANA)
        <input className="block border p-1" list="recurring-zones" value={draft.timezone}
          onChange={(e) => setDraft({ ...draft, timezone: e.target.value.trim() })} />
        <datalist id="recurring-zones">{knownZones().map(z => <option key={z} value={z} />)}</datalist>
      </label>
      {draft.slots.map((slot, index) => <fieldset key={index} className="flex flex-wrap gap-2 items-end border p-2">
        <legend>Slot {index + 1}</legend>
        {WEEKDAYS.map((day, bit) => <label key={day}>
          <input type="checkbox" checked={(slot.days & (1 << bit)) !== 0}
            onChange={(e) => setSlot(index, { days: e.target.checked ? slot.days | (1 << bit) : slot.days & ~(1 << bit) })} /> {day}
        </label>)}
        <label>Start (local)<input className="block border p-1" type="time" step="60" value={minuteLabel(slot.start_minute)}
          onChange={(e) => { const [h, m] = e.target.value.split(":").map(Number); if (Number.isInteger(h) && Number.isInteger(m)) setSlot(index, { start_minute: h * 60 + m }); }} /></label>
        <label>Duration (minutes, 1–1440)<input className="block border p-1" type="number" min="1" max="1440" step="1" value={slot.duration_minutes}
          onChange={(e) => setSlot(index, { duration_minutes: Number(e.target.value) })} /></label>
        {draft.slots.length > 1 && <button type="button" onClick={() => setDraft({ ...draft, slots: draft.slots.filter((_, i) => i !== index) })}>Remove slot</button>}
      </fieldset>)}
      {draft.slots.length < MAX_SLOTS && <button type="button"
        onClick={() => setDraft({ ...draft, slots: [...draft.slots, { days: 64, start_minute: 120, duration_minutes: 120 }] })}>Add slot</button>}
      {draftError && <p role="alert">{draftError}</p>}
      <p>A start that falls in a daylight-saving gap opens at the first valid time after it; a repeated local time opens once, at its earlier occurrence.</p>
      <button disabled={pending || !!draftError} type="submit">Save recurring window</button>
    </form>}
  </div>;
}
