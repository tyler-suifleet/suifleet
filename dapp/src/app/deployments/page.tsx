"use client";

import { useDeployments } from "@/hooks/useDeployments";
import { useDevices } from "@/hooks/useDevices";
import { STATUS_LABELS, STATUS_COLORS } from "@/lib/constants";
import { PageHeader } from "@/app/page";
import Link from "next/link";

export default function DeploymentsPage() {
  const { data: deployments = [], isLoading } = useDeployments();
  const { data: devices = [] } = useDevices();

  const deviceNames = Object.fromEntries(devices.map((d) => [d.objectId, d.name]));

  return (
    <div className="space-y-4 max-w-5xl">
      <div className="flex items-center justify-between">
        <PageHeader title="Deployments" subtitle="Firmware deployments to core device groups" />
        <Link
          href="/firmware"
          className="bg-[#ec7211] hover:bg-[#d4620e] text-white text-sm px-4 py-2 rounded"
        >
          Create deployment
        </Link>
      </div>

      <div className="bg-white border border-gray-200 rounded">
        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading…</div>
        ) : deployments.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-sm text-gray-500">No deployments found.</p>
            <Link href="/firmware" className="mt-2 inline-block text-xs text-[#0073bb] hover:underline">
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
                <Th>Target devices</Th>
                <Th>Created</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {deployments.map((d) => (
                <DeploymentRow key={d.deploymentId} d={d} deviceNames={deviceNames} />
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
  deviceNames,
}: {
  d: ReturnType<typeof useDeployments>["data"] extends (infer T)[] | undefined ? T : never;
  deviceNames: Record<string, string>;
}) {
  return (
    <>
      <tr className="hover:bg-gray-50 align-top">
        <td className="py-3 px-4 font-mono text-xs text-gray-400 max-w-[140px]">
          <span title={d.deploymentId}>{d.deploymentId.slice(0, 16)}…</span>
        </td>
        <td className="py-3 pr-4 font-medium">{d.firmwareName}</td>
        <td className="py-3 pr-4 text-gray-500">{d.version}</td>
        <td className="py-3 pr-4">
          <div className="space-y-1">
            {d.targetDevices.map((deviceId) => (
              <div key={deviceId} className="flex items-center gap-2">
                <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[0]}`}>
                  {STATUS_LABELS[0]}
                </span>
                <span className="text-xs text-gray-500">
                  {deviceNames[deviceId] ?? deviceId.slice(0, 12) + "…"}
                </span>
              </div>
            ))}
          </div>
        </td>
        <td className="py-3 pr-4 text-xs text-gray-400">
          {new Date(d.timestampMs).toLocaleString()}
        </td>
      </tr>
      <tr className="bg-gray-50 border-b border-gray-200">
        <td colSpan={5} className="px-4 pb-3">
          <div className="text-xs text-gray-400 space-x-4">
            <span>Blob: <span className="font-mono text-gray-500">{d.walrusBlobId}</span></span>
            <span>SHA256: <span className="font-mono text-gray-500">{d.sha256Hash.slice(0, 16)}…</span></span>
          </div>
        </td>
      </tr>
    </>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="py-2 px-4 font-medium">{children}</th>;
}
