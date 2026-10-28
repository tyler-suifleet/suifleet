"use client";

import { useFleetSelection } from "@/hooks/useFleetSelection";

import { useState } from "react";
import { buildDeploymentControlTx, type DeploymentControl } from "@/lib/deployment-control";
import { ControlEditor } from "@/components/DeploymentControlEditor";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { DeviceUpdateHistory } from "@/components/DeviceUpdateHistory";
import { DeploymentReclaimPanel } from "@/components/StorageReclaim";
import type { DeviceObject, DeviceGroup } from "@/lib/sui";
import { useDeployments } from "@/hooks/useDeployments";
import { useDeploymentStorage } from "@/hooks/useDeploymentStorage";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { buildDeleteDeploymentStepTx } from "@/lib/sui";
import { STATUS_LABELS, STATUS_COLORS } from "@/lib/constants";
import { PageHeader } from "@/components/ui";
import Link from "next/link";

export default function DeploymentsPage() {
  const { fleet } = useFleetSelection();
  const { data: deployments = [], isLoading, error, refetch } = useDeployments();
  const devices = useDevices(), groups = useGroups();
  const [view, setView] = useState<"devices" | "deployments">("devices");
  const account = useCurrentAccount();
  const client = useCurrentClient();
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
      {deployments.length > 0 && <DeploymentReclaimPanel deployments={deployments} />}
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
                  canDelete={fleet ? fleet.administrator && fleet.id === d.creator : account?.address === d.creator}
                  isPending={isPending}
                  onControl={(control) => signAndExecute(
                    { transaction: buildDeploymentControlTx(d.deploymentId, control, fleet) },
                    { onSuccess: () => refetch(), onError: (err) => alert(`Failed: ${err.message}`) }
                  )}
                  onDelete={async () => {
                    if (!confirm(`Delete deployment "${d.componentName} ${d.version}"? This cannot be undone.`)) return;
                    try { signAndExecute(
                      { transaction: await buildDeleteDeploymentStepTx(client, d.deploymentId, d.schemaVersion, fleet) },
                      {
                        onSuccess: () => setTimeout(() => refetch(), 3000),
                        onError: (err) => alert(`Failed: ${err.message}`),
                      }
                    ); } catch (error) { alert(`Failed: ${error instanceof Error ? error.message : String(error)}`); }
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
        <td className="py-3 px-4 font-medium">{d.componentName}</td>
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
              {d.receiptCleanup ? "Continue deletion" : "Delete"}
            </button>
          )}
        </td>
      </tr>
      <tr className="bg-gray-50 border-b border-gray-200">
        <td colSpan={5} className="px-4 pb-3">
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
          <details className="mt-2 text-xs text-gray-500"><summary className="cursor-pointer text-blue-700">Technical details</summary><p>Update ID: <span className="font-mono break-all">{d.deploymentId}</span></p></details>
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
