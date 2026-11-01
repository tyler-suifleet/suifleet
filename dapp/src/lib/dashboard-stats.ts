import type { DeviceHealth } from "./telemetry";
import type { DeploymentEvent } from "./sui";

/** Only current, verified assessments can establish health. Missing reports stay unknown. */
export function deviceHealthTile(deviceIds: string[], health: Record<string, DeviceHealth>) {
  const statuses = deviceIds.map(id => health[id]?.status);
  const healthy = statuses.filter(status => status === "healthy").length;
  const reported = statuses.filter(status => status === "healthy" || status === "degraded" || status === "offline").length;
  return reported === 0
    ? { label: "Registered devices", value: deviceIds.length, hint: "Health not reported" }
    : { label: "Devices (healthy / total)", value: `${healthy} / ${deviceIds.length}`,
        hint: reported < deviceIds.length ? `${deviceIds.length - reported} without verified health` : "Based on verified health reports" };
}

/** Waiting targets alone do not mean that admission is open. */
export function deploymentTile(deployments: DeploymentEvent[], now: number) {
  let active = 0, paused = 0;
  for (const deployment of deployments) {
    if (!deployment.resolvedDeviceIds.some(id => (deployment.statuses[id] ?? 0) === 0)) continue;
    const c = deployment.control;
    const time = BigInt(now);
    if (c.state === 2 || (BigInt(c.expires_ms) !== BigInt(0) && time >= BigInt(c.expires_ms))) continue;
    if (c.state === 1) { paused++; continue; }
    if (c.state !== 0 || time < BigInt(c.not_before_ms)) continue;
    if (BigInt(c.window_end_ms) !== BigInt(0) &&
        (time < BigInt(c.window_start_ms) || time >= BigInt(c.window_end_ms))) continue;
    active++;
  }
  return { active, hint: `${paused} paused; active counts deployments open for admission` };
}
