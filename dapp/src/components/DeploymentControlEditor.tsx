"use client";

import { useState } from "react";
import { controlReason, type DeploymentControl } from "@/lib/deployment-control";

/** Pause, cancel and schedule/notice editor for one deployment. */
export function ControlEditor({ control, canEdit, pending, onSave }: {
  control: DeploymentControl; canEdit: boolean; pending: boolean; onSave: (c: DeploymentControl) => void;
}) {
  const [draft, setDraft] = useState(control);
  const [editing, setEditing] = useState(false);
  const send = (c: DeploymentControl) => {
    try { onSave(c); } catch (error) { alert(error instanceof Error ? error.message : String(error)); }
  };
  const dateFields = [["not_before_ms", "Not before"], ["expires_ms", "Expires"], ["window_start_ms", "Window start"], ["window_end_ms", "Window end"]] as const;
  return <div className="py-3 text-xs space-y-2 text-gray-700">
    <p role="status">{controlReason(control)}</p>
    <p>Cancellation/pause/expiry stop admission. An admitted apply finishes; they never roll it back. Device deferral reasons and advance notices are recorded in its journal/log.</p>
    <p>Extending an expired schedule cannot reopen admission on devices that already observed expiry. Create a new deployment to reach the whole fleet.</p>
    <p>Not before: {displayTime(control.not_before_ms)} · Expires: {displayTime(control.expires_ms)} · Window: {displayTime(control.window_start_ms)} – {displayTime(control.window_end_ms)} · Notice: {Number(control.notice_ms) / 1000}s</p>
    {canEdit && control.state !== 2 && <div className="flex gap-3">
      <button disabled={pending} onClick={() => send({ ...control, state: control.state === 1 ? 0 : 1 })}>{control.state === 1 ? "Resume" : "Pause"}</button>
      <button disabled={pending} onClick={() => { if (confirm("Permanently cancel admission? Already admitted applies may finish.")) send({ ...control, state: 2 }); }}>Cancel permanently</button>
      <button disabled={pending} onClick={() => setEditing(!editing)}>Edit schedule / notice</button>
    </div>}
    {editing && <form className="flex flex-wrap gap-3" onSubmit={(e) => { e.preventDefault(); send(draft); }}>
      {dateFields.map(([field, label]) => <label key={field}>{label} (UTC)
        <input className="block border p-1" type="datetime-local" step="1" placeholder="2026-11-01T02:00:00" title="Leave empty for no limit"
          value={draft[field] === "0" || Number.isNaN(new Date(Number(draft[field])).getTime()) ? "" : new Date(Number(draft[field])).toISOString().slice(0, 19)}
          onChange={(e) => setDraft({ ...draft, [field]: e.target.value ? String(Date.parse(`${e.target.value}Z`)) : "0" })} />
      </label>)}
      <label>Advance notice (seconds, 0–604800)<input className="block border p-1" type="number" min="0" max="604800" step="1" value={Number(draft.notice_ms) / 1000}
        onChange={(e) => setDraft({ ...draft, notice_ms: String(Number(e.target.value) * 1000) })} /></label>
      <button disabled={pending} type="submit">Save schedule</button>
    </form>}
  </div>;
}
function displayTime(value: string) {
  if (value === "0") return "unset";
  const time = new Date(Number(value));
  return Number.isNaN(time.getTime()) ? `UTC milliseconds ${value}` : time.toISOString();
}
