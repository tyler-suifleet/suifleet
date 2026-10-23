// Per-device service views (SF-084) over the SF-082 verified health path.
//
// Values come only from a device's newest signed report, verified against its
// chain identity and the fleet data anchor (fetchChainHealth). Anything else is
// unavailable with a reason: an unverified, offline, delayed or missing report
// never supplies values, so the dashboard cannot show stale or partial state.
import { normalizeSuiAddress } from "@mysten/sui/utils";
import type { AlertsResult, FleetAlert } from "./alerts";
import { validServices, type BridgeHealth, type DeviceHealth, type QueueHealth, type ServicesHealth,
  type ShadowSyncHealth, type StreamHealth } from "./telemetry";

export type Unavailable = { state: "unavailable"; reason: string };
/** A service the device reported as not enabled (`null` in the signed section). */
export type Disabled = { state: "disabled" };

/**
 * Shadow documents are confidential and deliberately never leave the device
 * (docs/SHADOWS.md, docs/TELEMETRY.md): the verified path carries only shadow
 * counts and synchronization counters.
 */
export const SHADOW_DOCUMENTS_UNAVAILABLE: Unavailable = { state: "unavailable",
  reason: "Desired, reported and delta documents are kept on the device and are not published to Sui or Walrus; only counts and sync health are signed." };
/** The signed stream summary has no message timestamps. */
export const STREAM_TIMESTAMPS_UNAVAILABLE: Unavailable = { state: "unavailable",
  reason: "The device's signed stream summary does not include oldest/newest message times." };

export interface StreamView extends StreamHealth {
  /** Export checkpoint: messages appended but not yet exported, and its failures. */
  exportState: { state: "exported"; health: NonNullable<StreamHealth["export"]> } | { state: "no-export" };
  timestamps: Unavailable;
}

export interface ServicesView {
  state: "verified";
  /** Device clock when the verified report was taken. */
  observedMs: number;
  /** Chain inclusion time of the verified report. */
  receivedMs: number;
  messaging: { state: "verified"; subscriptions: number; queued: number; refused: number; dropped: number; queues: QueueHealth[]; omitted: number } | Disabled;
  bridge: ({ state: "verified" } & BridgeHealth) | Disabled;
  streams: { state: "verified"; streams: StreamView[]; omitted: number } | Disabled;
  shadows: { state: "verified"; shadows: number; notificationsRefused: number; notificationsRefusedRecent: number;
    sync: ShadowSyncHealth[]; omitted: number; documents: Unavailable } | Disabled;
}

/**
 * The service view of one device, or why there is none. Only `healthy` and
 * `degraded` reports that are current (not offline, not delayed) and carry a
 * services section of exactly the version-1 shape yield values.
 */
export function deviceServicesView(health: DeviceHealth | undefined): ServicesView | Unavailable {
  if (!health) return { state: "unavailable", reason: "Device health has not been read yet." };
  const why = health.reasons.length ? `: ${health.reasons.join("; ")}` : "";
  switch (health.status) {
    case "unavailable": return { state: "unavailable", reason: `Health could not be verified${why}` };
    case "unverified": return { state: "unavailable", reason: `The newest report was rejected${why}` };
    case "unreported": return { state: "unavailable", reason: `No verified report${why}` };
    case "offline": return { state: "unavailable", reason: `The newest verified report is too old to describe the device now${why}` };
    case "healthy": case "degraded": break;
    default: return { state: "unavailable", reason: "Unknown health status." };
  }
  if (health.codes.includes("delayed")) {
    return { state: "unavailable", reason: "The newest report was buffered long before it reached the chain; its service values are not current." };
  }
  if (health.observedMs === undefined || health.lastSeenMs === undefined) {
    return { state: "unavailable", reason: "The verified report has no observation or chain time." };
  }
  if (health.services === undefined) {
    return { state: "unavailable", reason: "The device's verified report has no services section (no pub/sub, bridge, streams or shadows enabled, or an older daemon)." };
  }
  // Defense in depth: verifyReport already validated the section; refuse a
  // value that did not come through it unchanged.
  if (!validServices(health.services)) return { state: "unavailable", reason: "The services section is malformed." };
  return servicesView(health.services, health.observedMs, health.lastSeenMs);
}

function servicesView(s: ServicesHealth, observedMs: number, receivedMs: number): ServicesView {
  return {
    state: "verified", observedMs, receivedMs,
    messaging: s.messaging ? { state: "verified", ...s.messaging } : { state: "disabled" },
    bridge: s.bridge ? { state: "verified", ...s.bridge } : { state: "disabled" },
    streams: s.streams ? { state: "verified", omitted: s.streams.omitted, streams: s.streams.streams.map((stream) => ({
      ...stream,
      exportState: stream.export ? { state: "exported", health: stream.export } : { state: "no-export" },
      timestamps: STREAM_TIMESTAMPS_UNAVAILABLE,
    })) } : { state: "disabled" },
    shadows: s.shadows ? { state: "verified", shadows: s.shadows.shadows, notificationsRefused: s.shadows.notifications_refused,
      notificationsRefusedRecent: s.shadows.notifications_refused_recent, sync: s.shadows.sync, omitted: s.shadows.omitted,
      documents: SHADOW_DOCUMENTS_UNAVAILABLE } : { state: "disabled" },
  };
}

export type DeviceAlerts = { state: "verified"; items: FleetAlert[] } | Unavailable;

/** Active alerts for one device. A fleet result that is unavailable is unavailable here too. */
export function deviceAlerts(result: AlertsResult, deviceId: string): DeviceAlerts {
  if (result.status !== "ok") return { state: "unavailable", reason: result.reason };
  const device = normalizeSuiAddress(deviceId);
  return { state: "verified", items: result.items.filter((alert) => normalizeSuiAddress(alert.deviceId) === device) };
}

/** A UTC time for display; values outside the Date range are shown raw. */
export function utc(ms: number | null): string {
  if (ms === null) return "never";
  const time = new Date(ms);
  return Number.isNaN(time.getTime()) ? `UTC milliseconds ${ms} (out of range)` : time.toISOString();
}
