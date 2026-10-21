import type { DeploymentEvent, DeviceGroup, DeviceObject } from "./sui";

export interface DeviceUpdate {
  deviceId: string;
  deviceName: string;
  currentGroups: DeviceGroup[] | null;
  deployment: DeploymentEvent;
  outcome: "Applied" | "Failed" | "No receipt" | "Unverified";
}

/** Identity is always the object ID: duplicate display names remain distinct. */
export function deviceUpdates(deployments: DeploymentEvent[], devices: DeviceObject[], groups?: DeviceGroup[]): DeviceUpdate[] {
  const names = new Map(devices.map(device => [device.objectId, device.name]));
  return deployments.flatMap(deployment => {
    const ids = new Set([...deployment.targetDevices, ...deployment.resolvedDeviceIds,
      ...Object.keys(deployment.receiptsVerified ? deployment.statuses : {})]);
    return [...ids].map(deviceId => ({
      deviceId, deviceName: names.get(deviceId) ?? "Unavailable device",
      currentGroups: groups ? groups.filter(group => group.deviceIds.includes(deviceId)) : null,
      deployment,
      outcome: !deployment.receiptsVerified ? "Unverified" : deployment.statuses[deviceId] === 1 ? "Applied"
        : deployment.statuses[deviceId] === 2 ? "Failed" : "No receipt",
    } satisfies DeviceUpdate));
  });
}

export function filterUpdates(rows: DeviceUpdate[], deviceSearch: string, groupSearch: string, sort: string,
  deviceId = "", groupId = ""): DeviceUpdate[] {
  const device = deviceSearch.trim().toLowerCase(), group = groupSearch.trim().toLowerCase();
  return rows.filter(row => (!deviceId || row.deviceId === deviceId)
    && row.deviceName.toLowerCase().includes(device)
    && (!groupId || row.currentGroups?.some(item => item.objectId === groupId))
    && (!group || row.currentGroups?.some(item => item.name.toLowerCase().includes(group))))
    .sort((a, b) => {
      const newest = b.deployment.timestampMs - a.deployment.timestampMs;
      if (sort === "newest" && newest) return newest;
      const name = a.deviceName.localeCompare(b.deviceName);
      return (sort === "name-desc" ? -name : name) || a.deviceId.localeCompare(b.deviceId)
        || newest || a.deployment.deploymentId.localeCompare(b.deployment.deploymentId);
    });
}
