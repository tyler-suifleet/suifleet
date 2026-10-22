"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { DeploymentEvent, DeviceGroup, DeviceObject } from "@/lib/sui";
import { deviceUpdates, filterUpdates } from "@/lib/deployment-history";

export function DeviceUpdateHistory({ deployments, devices, groups, deviceId, loading = false, error }: {
  deployments: DeploymentEvent[]; devices: DeviceObject[]; groups?: DeviceGroup[];
  deviceId?: string; loading?: boolean; error?: string;
}) {
  const [deviceSearch, setDeviceSearch] = useState("");
  const [groupSearch, setGroupSearch] = useState("");
  const [sort, setSort] = useState("name-asc");
  const [linkedDevice, setLinkedDevice] = useState("");
  const [linkedGroup, setLinkedGroup] = useState("");
  useEffect(() => {
    if (deviceId) return;
    const query = new URLSearchParams(window.location.search);
    setLinkedDevice(query.get("device") ?? ""); setLinkedGroup(query.get("group") ?? "");
  }, [deviceId]);
  const rows = filterUpdates(deviceUpdates(deployments, devices, groups), deviceSearch, groupSearch, sort, deviceId ?? linkedDevice, linkedGroup);
  return <section className="space-y-3" aria-label="Device update history">
    <div className="flex flex-wrap items-end gap-3">
      {!deviceId && <label className="text-sm">Search device name<input type="search" value={deviceSearch}
        onChange={event => { setDeviceSearch(event.target.value); setLinkedDevice(""); }}
        className="mt-1 block rounded border px-3 py-2" placeholder="Device name" /></label>}
      <label className="text-sm">Search current device group<input type="search" value={groupSearch}
        onChange={event => { setGroupSearch(event.target.value); setLinkedGroup(""); }}
        className="mt-1 block rounded border px-3 py-2" placeholder="Group name" /></label>
      <label className="text-sm">Sort<select value={sort} onChange={event => setSort(event.target.value)} className="mt-1 block rounded border px-3 py-2">
        <option value="name-asc">Device name A–Z</option><option value="name-desc">Device name Z–A</option><option value="newest">Newest update</option>
      </select></label>
      {(linkedDevice || linkedGroup) && <button className="text-sm text-blue-700 underline" onClick={() => { setLinkedDevice(""); setLinkedGroup(""); }}>Clear linked filter</button>}
    </div>
    <p className="text-xs text-gray-500">Groups show current membership. Update details retain the original target groups. Applied and Failed are verified device receipts, not live device health.</p>
    {error ? <p role="alert" className="text-sm text-red-700">Update history unavailable: {error}</p>
      : loading ? <p role="status">Loading device update history…</p>
      : rows.length === 0 ? <p className="p-6 text-sm text-gray-500">No device updates match these filters. Deployments without known device targets are available under All deployments.</p>
      : <div className="overflow-x-auto rounded border bg-white"><table className="w-full text-left text-sm">
        <thead className="bg-gray-50 text-xs text-gray-600"><tr>{["Device", "Current groups", "Component", "Version", "Result", "Created", "Details"].map(title => <th key={title} className="p-3 font-medium">{title}</th>)}</tr></thead>
        <tbody className="divide-y">{rows.map(row => <tr key={`${row.deviceId}/${row.deployment.deploymentId}`}>
          <td className="p-3"><Link href={`/devices?device=${encodeURIComponent(row.deviceId)}`} className="font-medium text-blue-700 underline">{row.deviceName}</Link></td>
          <td className="p-3">{row.currentGroups === null ? "Membership unavailable" : row.currentGroups.length === 0 ? "No group"
            : row.currentGroups.map(group => <a key={group.objectId} href={`/deployments?group=${encodeURIComponent(group.objectId)}`} className="mr-2 inline-block text-blue-700 underline">{group.name}</a>)}</td>
          <td className="p-3 font-medium">{row.deployment.componentName}</td><td className="p-3">{row.deployment.version}</td>
          <td className="p-3"><span className={`rounded px-2 py-1 text-xs ${row.outcome === "Applied" ? "bg-green-100 text-green-800" : row.outcome === "Failed" ? "bg-red-100 text-red-800" : "bg-gray-100 text-gray-700"}`}>{row.outcome}</span></td>
          <td className="p-3 text-xs text-gray-500">{new Date(row.deployment.timestampMs).toLocaleString()}</td>
          <td className="p-3"><details><summary className="cursor-pointer text-blue-700">Update details</summary>
            <dl className="mt-2 max-w-xs space-y-1 break-all text-xs text-gray-500">
              <dt>Device ID</dt><dd>{row.deviceId}</dd><dt>Deployment ID</dt><dd>{row.deployment.deploymentId}</dd>
              <dt>Original target groups</dt><dd>{row.deployment.targetGroups.length ? row.deployment.targetGroups.map(id => groups?.find(group => group.objectId === id)?.name ?? `Unavailable group (${id})`).join(", ") : "Direct device target"}</dd>
            </dl>
          </details></td>
        </tr>)}</tbody>
      </table></div>}
  </section>;
}
