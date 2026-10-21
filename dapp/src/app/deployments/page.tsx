"use client";

import { useState } from "react";
import { buildDeploymentControlTx, controlReason, type DeploymentControl } from "@/lib/deployment-control";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { DeviceUpdateHistory } from "@/components/DeviceUpdateHistory";
import type { DeviceObject, DeviceGroup } from "@/lib/sui";
import { useDeployments } from "@/hooks/useDeployments";
import { useDeploymentStorage } from "@/hooks/useDeploymentStorage";
import { useCurrentAccount } from "@mysten/dapp-kit-react";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildDeleteDeploymentTx } from "@/lib/sui";
import { STATUS_LABELS, STATUS_COLORS } from "@/lib/constants";
import { PageHeader } from "@/components/ui";
import Link from "next/link";

export default function DeploymentsPage() {
  const { data: deployments = [], isLoading, error, refetch } = useDeployments();
  const devices = useDevices(), groups = useGroups();
  const [view, setView] = useState<"devices" | "deployments">("devices");
  const account = useCurrentAccount();
  const { mutate: signAndExecute, isPending } = useFleetTransaction();

  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between">
        <PageHeader title="Deployments" subtitle="Device update history, outcomes and deployment controls" />
        <Link href="/rollouts" className="text-sm text-blue-600">Staged rollouts</Link>
        <Link
          href="/firmware"
          className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white text-sm px-4 py-2 rounded"
        >
          Deploy component
        </Link>
      </div>

      <nav aria-label="Deployment views" className="flex gap-2">
        <button aria-pressed={view === "devices"} onClick={() => setView("devices")} className="rounded border px-3 py-2 text-sm">By device</button>
        <button aria-pressed={view === "deployments"} onClick={() => setView("deployments")} className="rounded border px-3 py-2 text-sm">All deployments</button>
      </nav>
      {view === "devices" && <>
        {groups.error && <p role="alert">Group membership unavailable: {groups.error.message}</p>}
        <DeviceUpdateHistory deployments={deployments} devices={devices.data ?? []} groups={groups.error ? undefined : groups.data}
          loading={isLoading || devices.isLoading || groups.isLoading} error={error?.message ?? devices.error?.message} />
      </>}
      {view === "deployments" && <>
      {error && <p role="alert" className="text-sm text-red-600">Failed to load deployments: {error.message}</p>}
      <div className="bg-white border border-gray-200 rounded">
        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading…</div>
        ) : deployments.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-sm text-gray-500">No deployments found.</p>
            <Link href="/firmware" className="mt-2 inline-block text-xs text-[#4DA2FF] hover:underline">
              Create your first deployment
            </Link>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 bg-gray-50">
                <Th>Update</Th>
                <Th>Component</Th>
                <Th>Version</Th>
                <Th>Targets</Th>
                <Th>Created</Th>
                <Th>Actions</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {deployments.map((d) => (
                <DeploymentRow
                  key={d.deploymentId}
                  d={d}
                  devices={devices.data ?? []}
                  groups={groups.data ?? []}
                  canDelete={account?.address === d.creator}
                  isPending={isPending}
                  onControl={(control) => signAndExecute(
                    { transaction: buildDeploymentControlTx(d.deploymentId, control) },
                    { onSuccess: () => refetch(), onError: (err) => alert(`Failed: ${err.message}`) }
                  )}
                  onDelete={() => {
                    if (!confirm(`Delete deployment "${d.componentName} ${d.version}"? This cannot be undone.`)) return;
                    signAndExecute(
                      { transaction: buildDeleteDeploymentTx(d.deploymentId, d.schemaVersion) },
                      {
                        onSuccess: () => setTimeout(() => refetch(), 3000),
                        onError: (err) => alert(`Failed: ${err.message}`),
                      }
                    );
                  }}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
      </>}
    </div>
  );
}

function DeploymentRow({
  d,
  devices,
  groups,
  canDelete,
  isPending,
  onDelete,
  onControl,
}: {
  d: ReturnType<typeof useDeployments>["data"] extends (infer T)[] | undefined ? T : never;
  devices: DeviceObject[];
  groups: DeviceGroup[];
  canDelete: boolean;
  isPending: boolean;
  onDelete: () => void;
  onControl: (control: DeploymentControl) => void;
}) {
  return (
    <>
      <tr className="hover:bg-gray-50 align-top">
        <td className="py-3 px-4 font-mono text-xs text-gray-400 max-w-[140px]">
          <details><summary className="cursor-pointer text-blue-700">Technical details</summary><span className="break-all">{d.deploymentId}</span></details>
        </td>
        <td className="py-3 pr-4 font-medium">{d.componentName}</td>
        <td className="py-3 pr-4 text-gray-500">{d.version}</td>
        <td className="py-3 pr-4">
          <div className="space-y-1">
            {d.targetGroups.map((id) => (
              <div key={id} className="flex items-center gap-2">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">group</span>
                <a href={`/deployments?group=${encodeURIComponent(id)}`} className="text-xs text-blue-700 underline">{groups.find(group => group.objectId === id)?.name ?? "Unavailable group"}</a>
              </div>
            ))}
            {d.targetDevices.map((id) => (
              <div key={id} className="flex items-center gap-2">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-100 text-purple-700">device</span>
                <Link href={`/devices?device=${encodeURIComponent(id)}`} className="text-xs text-blue-700 underline">{devices.find(device => device.objectId === id)?.name ?? "Unavailable device"}</Link>
              </div>
            ))}
          </div>
        </td>
        <td className="py-3 pr-4 text-xs text-gray-400">
          {new Date(d.timestampMs).toLocaleString()}
        </td>
        <td className="py-3">
          {canDelete && (
            <button
              disabled={isPending}
              onClick={onDelete}
              className="text-xs text-red-500 hover:text-red-700 disabled:opacity-40"
            >
              Delete
            </button>
          )}
        </td>
      </tr>
      <tr className="bg-gray-50 border-b border-gray-200">
        <td colSpan={6} className="px-4 pb-3">
          <ControlEditor key={`${d.deploymentId}/${d.control.revision}`} control={d.control} canEdit={canDelete} pending={isPending} onSave={onControl} />
          <div className="text-xs text-gray-400 space-x-4">
            {d.receiptsVerified ? d.resolvedDeviceIds.map((id) => (
              <span key={id} className={`inline-flex px-2 py-1 rounded ${STATUS_COLORS[d.statuses[id] ?? 0]}`} title={id}>
                {devices.find(device => device.objectId === id)?.name ?? "Unavailable device"}: {STATUS_LABELS[d.statuses[id] ?? 0]}
              </span>
            )) : (
              <span
                className="inline-flex px-2 py-1 rounded bg-gray-200 text-gray-700"
                title="No receipts from the current contracts. Older contract versions let any address write legacy statuses, or accepted reports from since-revoked device keys, so none are shown. A device report through the current contracts discards them and adds a verified receipt; redeploy if you need one now."
              >
                Unverified: no signed device receipts yet
              </span>
            )}
            {d.recipeBlobId && <span>Recipe: <span className="font-mono text-gray-500">{d.recipeBlobId}</span></span>}
            {d.schemaVersion === "2" ? <span>Catalog deployment · each device selects its platform recipe</span> : d.recipeBlobId ? <StorageBadge recipeBlobId={d.recipeBlobId} /> : <span>No recipe blob</span>}
            {d.artifactSha256 && <span>SHA256: <span className="font-mono text-gray-500">{d.artifactSha256.slice(0, 16)}…</span></span>}
          </div>
        </td>
      </tr>
    </>
  );
}

/** Flag deployments whose blobs devices can no longer fetch (storage period ended). */
function StorageBadge({ recipeBlobId }: { recipeBlobId: string }) {
  const { data } = useDeploymentStorage(recipeBlobId);
  if (!data) return null;
  const missing = [data.recipe === "unavailable" && "recipe", data.artifact === "unavailable" && "artifact"].filter(Boolean);
  if (missing.length === 0) {
    if (data.confidential) return <span className="inline-flex px-2 py-1 rounded bg-blue-50 text-blue-700" title="Seal-encrypted; only granted devices and accounts can decrypt">Confidential recipe stored</span>;
    return data.recipe === "available" && data.artifact === "available"
      ? <span className="inline-flex px-2 py-1 rounded bg-green-50 text-green-700">Storage available · public</span>
      : null;
  }
  return (
    <span className="inline-flex px-2 py-1 rounded bg-red-100 text-red-800" title="Re-upload and redeploy; devices that have not applied it will report Failed">
      {missing.join(" and ")} expired or unavailable on Walrus
    </span>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="py-2 px-4 font-medium">{children}</th>;
}

function ControlEditor({ control, canEdit, pending, onSave }: {
  control: DeploymentControl; canEdit: boolean; pending: boolean; onSave: (c: DeploymentControl) => void;
}) {
  const [draft, setDraft] = useState(control);
  const [editing, setEditing] = useState(false);
  const send = (c: DeploymentControl) => {
    try { onSave(c); } catch (error) { alert(error instanceof Error ? error.message : String(error)); }
  };
  const dateFields = ["not_before_ms", "expires_ms", "window_start_ms", "window_end_ms"] as const;
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
      {dateFields.map((field) => <label key={field}>{field.replaceAll("_", " ")} (UTC)
        <input aria-label={field} className="block border p-1" type="datetime-local" step="1"
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
