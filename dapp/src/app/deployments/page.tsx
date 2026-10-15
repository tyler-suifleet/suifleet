"use client";

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
  const account = useCurrentAccount();
  const { mutate: signAndExecute, isPending } = useFleetTransaction();

  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between">
        <PageHeader title="Deployments" subtitle="Firmware deployments to core device groups" />
        <Link
          href="/firmware"
          className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white text-sm px-4 py-2 rounded"
        >
          Deploy component
        </Link>
      </div>

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
                <Th>Deployment ID</Th>
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
                  canDelete={account?.address === d.creator}
                  isPending={isPending}
                  onDelete={() => {
                    if (!confirm(`Delete deployment "${d.componentName} ${d.version}"? This cannot be undone.`)) return;
                    signAndExecute(
                      { transaction: buildDeleteDeploymentTx(d.deploymentId) },
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
    </div>
  );
}

function DeploymentRow({
  d,
  canDelete,
  isPending,
  onDelete,
}: {
  d: ReturnType<typeof useDeployments>["data"] extends (infer T)[] | undefined ? T : never;
  canDelete: boolean;
  isPending: boolean;
  onDelete: () => void;
}) {
  return (
    <>
      <tr className="hover:bg-gray-50 align-top">
        <td className="py-3 px-4 font-mono text-xs text-gray-400 max-w-[140px]">
          <span title={d.deploymentId}>{d.deploymentId.slice(0, 16)}…</span>
        </td>
        <td className="py-3 pr-4 font-medium">{d.componentName}</td>
        <td className="py-3 pr-4 text-gray-500">{d.version}</td>
        <td className="py-3 pr-4">
          <div className="space-y-1">
            {d.targetGroups.map((id) => (
              <div key={id} className="flex items-center gap-2">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">group</span>
                <span className="text-xs text-gray-500 font-mono">{id.slice(0, 12)}…</span>
              </div>
            ))}
            {d.targetDevices.map((id) => (
              <div key={id} className="flex items-center gap-2">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-100 text-purple-700">device</span>
                <span className="text-xs text-gray-500 font-mono">{id.slice(0, 12)}…</span>
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
          <div className="text-xs text-gray-400 space-x-4">
            {d.receiptsVerified ? d.resolvedDeviceIds.map((id) => (
              <span key={id} className={`inline-flex px-2 py-1 rounded ${STATUS_COLORS[d.statuses[id] ?? 0]}`} title={id}>
                {id.slice(0, 10)}…: {STATUS_LABELS[d.statuses[id] ?? 0]}
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
            <StorageBadge recipeBlobId={d.recipeBlobId} />
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
    return data.recipe === "available" && data.artifact === "available"
      ? <span className="inline-flex px-2 py-1 rounded bg-green-50 text-green-700">Storage available</span>
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
