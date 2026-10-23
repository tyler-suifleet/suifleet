import { TELEMETRY_URL } from "@/lib/telemetry";

/**
 * Fleet alerts (SF-020) as the indexer derives them from verified telemetry
 * and indexed receipts. Unlike device health, the dApp cannot re-derive these
 * from the chain alone, so they are shown as reported by the indexer. They are
 * active incidents: a failed deployment that a later applied deployment of the
 * same component demonstrably recovered (SF-098) is no longer one, but its
 * Failed receipt stays in the deployment history.
 */
export type AlertKind = "component-broken" | "deployment-failed" | "device-offline" | "device-degraded" | "deployment-pending"
  // Service health (SF-085): pub/sub, MQTT bridge, streams and shadow sync.
  | "messaging-backpressure" | "messaging-loss" | "bridge-disconnected" | "stream-near-limit" | "stream-data-lost"
  | "export-failing" | "shadow-sync-failing" | "shadow-conflict";
export const ALERT_KINDS: readonly AlertKind[] = ["component-broken", "deployment-failed", "device-offline", "device-degraded", "deployment-pending",
  "messaging-backpressure", "messaging-loss", "bridge-disconnected", "stream-near-limit", "stream-data-lost",
  "export-failing", "shadow-sync-failing", "shadow-conflict"];

export interface FleetAlert {
  key: string;
  kind: AlertKind;
  severity: "critical" | "warning";
  deviceId: string;
  deviceName: string;
  deploymentId?: string;
  component?: string;
  message: string;
  action: string;
  sinceMs?: number;
}

export type AlertsResult = { status: "ok"; items: FleetAlert[]; serverNowMs: number } | { status: "unavailable"; reason: string };

export const ALERTS_REFRESH_MS = 30_000;
export const ALERTS_STALE_MS = 2 * ALERTS_REFRESH_MS;
const MAX_ALERTS = 1000;

const text = (value: unknown, limit: number, name: string): string => {
  if (typeof value !== "string" || value.length > limit) throw new Error(`alert ${name} is not a string of at most ${limit} characters`);
  return value;
};

/** Validate one alert from the indexer; anything unexpected is refused. */
export function parseAlert(raw: unknown): FleetAlert {
  if (!raw || typeof raw !== "object") throw new Error("alert is not an object");
  const a = raw as Record<string, unknown>;
  const kind = text(a.kind, 64, "kind") as AlertKind;
  if (!ALERT_KINDS.includes(kind)) throw new Error(`unknown alert kind ${kind}`);
  const severity = text(a.severity, 16, "severity");
  if (severity !== "critical" && severity !== "warning") throw new Error(`unknown severity ${severity}`);
  const optional = (value: unknown, name: string) => value === undefined || value === null ? undefined : text(value, 200, name);
  const since = a.since_ms;
  if (since !== undefined && since !== null && (typeof since !== "number" || !Number.isSafeInteger(since) || since < 0)) throw new Error("alert since_ms is invalid");
  return {
    key: text(a.key, 400, "key"),
    kind,
    severity,
    deviceId: text(a.device_id, 80, "device_id"),
    deviceName: text(a.device_name, 200, "device_name"),
    deploymentId: optional(a.deployment_id, "deployment_id"),
    component: optional(a.component, "component"),
    message: text(a.message, 400, "message"),
    action: text(a.action, 400, "action"),
    sinceMs: typeof since === "number" ? since : undefined,
  };
}

/** Alerts for the owner's devices; never rejects. */
export async function fetchAlerts(owner: string, url = TELEMETRY_URL, token = "", fetcher: typeof fetch = fetch): Promise<AlertsResult> {
  if (!url) return { status: "unavailable", reason: "no fleet indexer configured (NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL)" };
  try {
    const query = new URLSearchParams({ owner });
    const response = await fetcher(`${url.replace(/\/+$/, "")}/v1/alerts?${query}`, { headers: token ? { authorization: `Bearer ${token}` } : {} });
    if (!response.ok) return { status: "unavailable", reason: `fleet indexer answered ${response.status}` };
    const body = await response.json() as { items?: unknown; server_time_ms?: unknown };
    if (!Array.isArray(body.items) || body.items.length > MAX_ALERTS || typeof body.server_time_ms !== "number") {
      return { status: "unavailable", reason: "fleet indexer returned an invalid alert list" };
    }
    return { status: "ok", items: body.items.map(parseAlert), serverNowMs: body.server_time_ms };
  } catch (error) {
    return { status: "unavailable", reason: `alerts unavailable: ${error instanceof Error ? error.message : String(error)}` };
  }
}

/** A result older than the stale window is not shown as current. */
export function currentAlerts(result: AlertsResult | undefined, updatedAt: number, failed: boolean, now: number): AlertsResult {
  if (failed) return { status: "unavailable", reason: "the last alert refresh failed" };
  if (!result) return { status: "unavailable", reason: "alerts not loaded yet" };
  if (result.status === "ok" && now - updatedAt > ALERTS_STALE_MS) return { status: "unavailable", reason: "alerts have not refreshed recently" };
  return result;
}

/** Failed device results in the on-chain deployment history (SF-098), whether or not still an active incident. */
export function historicalFailures(deployments: readonly { statuses: Record<string, number> }[]): number {
  return deployments.reduce((sum, d) => sum + Object.values(d.statuses).filter((s) => s === 2).length, 0);
}
