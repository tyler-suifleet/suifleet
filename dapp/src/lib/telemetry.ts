import { batchFields } from "./batch-fields";
// Device health is read from signed on-chain summaries. Explicit HTTP reader
// arguments remain available for customer-owned reference integrations only.
import { bcs } from "@mysten/sui/bcs";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { fromBase64, normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { isValidPersonalMessageSignature } from "@mysten/sui/verify";
import { device_registry_IdentityBcs as IdentityBcs } from "./fleet-protocol";
import { moveType, requireManifest } from "./deployment-manifest";

/** Product reads never select an HTTP indexer from build environment. */
export const TELEMETRY_URL = "";
export const TELEMETRY_SCHEMA = 1;
/** A snapshot with a services section (SF-085); without one it stays schema 1. */
export const TELEMETRY_SCHEMA_SERVICES = 2;
/** Version 2 (SF-084) adds each stream's retained sequence range and export checkpoint. */
export const SERVICES_VERSION = 2;
export const SERVICES_VERSIONS: readonly number[] = [1, 2];
const MAX_SERVICE_ENTRIES = 32;
export const MIN_INTERVAL_SECONDS = 10;
export const MAX_INTERVAL_SECONDS = 3600;
export const OFFLINE_INTERVALS = 3;
export const DELAYED_INTERVALS = 2;

export interface ComponentHealth {
  name: string;
  version: string;
  deployment_id: string;
  installed: boolean;
  supervised: boolean;
  phase: string | null;
  restarts: number | null;
  last_exit: string | null;
  since_ms: number | null;
}

export interface QueueHealth {
  principal: string; subscriptions: number; queued: number; fill_percent: number; full_since_ms: number | null;
  refused: number; refused_recent: number; dropped: number; dropped_recent: number;
}
export interface BridgeHealth {
  connected: boolean; since_ms: number; connects: number; refused: number; refused_recent: number;
  rejected: number; rejected_recent: number; last_error: string | null;
}
export interface ExportHealth {
  /** Version 2: the last acknowledged sequence; null before the first acknowledgement. */
  checkpoint?: number | null;
  lag: number; lost: number; lost_recent: number; batches: number; failures: number; consecutive_failures: number;
  last_success_ms: number | null; last_error: string | null;
}
export interface StreamHealth {
  name: string; bytes: number; max_size: number; fill_percent: number; messages: number;
  /** Version 2: first and last retained sequence; both null exactly when empty. */
  oldest?: number | null; newest?: number | null;
  overwrite: boolean;
  rejected: number; rejected_recent: number; export: ExportHealth | null;
}
export interface ShadowSyncHealth {
  name: string; conflicts: number; conflicts_recent: number; failures: number; consecutive_failures: number;
  last_success_ms: number | null; last_error: string | null;
}
/**
 * On-device service health (SF-085): counters and states of durable pub/sub,
 * its MQTT bridge, streams and shadow sync. Never payloads, topics, URLs or
 * credentials.
 */
export interface ServicesHealth {
  version: number;
  messaging: { subscriptions: number; queued: number; refused: number; dropped: number; queues: QueueHealth[]; omitted: number } | null;
  bridge: BridgeHealth | null;
  streams: { streams: StreamHealth[]; omitted: number } | null;
  shadows: { shadows: number; notifications_refused: number; notifications_refused_recent: number; sync: ShadowSyncHealth[]; omitted: number } | null;
}

/**
 * The only values a service `last_error` may take: daemons report error
 * categories, never texts, which could name topics, filters, hosts or shadow
 * keys (interfaces/telemetry-cases.json `service_error_categories`).
 */
export const SERVICE_ERROR_CATEGORIES: readonly string[] = [
  "conflict", "connection-failed", "connection-lost", "connection-refused", "credentials-unreadable", "dns",
  "document-refused", "http-client-error", "http-redirect", "http-server-error", "invalid-response", "other",
  "protocol", "queue-full", "signing", "storage", "subscription-refused", "timeout", "tls", "unauthorized",
];

type Field = "count" | "percent" | "bool" | "name" | "error" | "time?" | ((value: unknown) => boolean);

const isCount = (v: unknown) => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const utf8Length = (v: string) => new TextEncoder().encode(v).length;
const FIELD: Record<Exclude<Field, (value: unknown) => boolean>, (v: unknown) => boolean> = {
  count: isCount,
  percent: (v) => isCount(v) && (v as number) <= 100,
  bool: (v) => typeof v === "boolean",
  name: (v) => typeof v === "string" && utf8Length(v) <= 128,
  error: (v) => v === null || (typeof v === "string" && SERVICE_ERROR_CATEGORIES.includes(v)),
  "time?": (v) => v === null || isCount(v),
};

/** Exactly these keys, each valid (the daemon and indexer refuse unknown fields too). */
function shaped(value: unknown, fields: Record<string, Field>): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === Object.keys(fields).length && keys.every((key) => {
    const check = fields[key];
    if (!check) return false;
    return (typeof check === "function" ? check : FIELD[check])((value as Record<string, unknown>)[key]);
  });
}
const list = (fields: Record<string, Field>) => (v: unknown) => Array.isArray(v) && v.length <= MAX_SERVICE_ENTRIES && v.every((e) => shaped(e, fields));
const orNull = (fields: Record<string, Field>) => (v: unknown) => v === null || shaped(v, fields);

const STREAM_V1: Record<string, Field> = { name: "name", bytes: "count", max_size: "count", fill_percent: "percent",
  messages: "count", overwrite: "bool", rejected: "count", rejected_recent: "count",
  export: orNull({ lag: "count", lost: "count", lost_recent: "count", batches: "count", failures: "count",
    consecutive_failures: "count", last_success_ms: "time?", last_error: "error" }) };
const STREAM_V2: Record<string, Field> = { ...STREAM_V1, oldest: "time?", newest: "time?",
  export: orNull({ checkpoint: "time?", lag: "count", lost: "count", lost_recent: "count", batches: "count", failures: "count",
    consecutive_failures: "count", last_success_ms: "time?", last_error: "error" }) };
/** Version 2 ranges: set exactly when there are messages, ordered, with room
 * for every message (sequences skipped by a crash leave gaps). */
function positioned(stream: StreamHealth): boolean {
  const { oldest, newest, messages } = stream;
  if (oldest === null || newest === null) return oldest === newest && messages === 0;
  return oldest! <= newest! && messages >= 1 && messages - 1 <= newest! - oldest!;
}

const servicesFields = (version: 1 | 2): Record<string, Field> => ({
  version: (v) => v === version,
  messaging: orNull({ subscriptions: "count", queued: "count", refused: "count", dropped: "count", omitted: "count",
    queues: list({ principal: "name", subscriptions: "count", queued: "count", fill_percent: "percent", full_since_ms: "time?",
      refused: "count", refused_recent: "count", dropped: "count", dropped_recent: "count" }) }),
  bridge: orNull({ connected: "bool", since_ms: "count", connects: "count", refused: "count", refused_recent: "count",
    rejected: "count", rejected_recent: "count", last_error: "error" }),
  streams: orNull({ omitted: "count", streams: version === 1 ? list(STREAM_V1)
    : (v) => list(STREAM_V2)(v) && (v as StreamHealth[]).every(positioned) }),
  shadows: orNull({ shadows: "count", notifications_refused: "count", notifications_refused_recent: "count", omitted: "count",
    sync: list({ name: "name", conflicts: "count", conflicts_recent: "count", failures: "count", consecutive_failures: "count",
      last_success_ms: "time?", last_error: "error" }) }),
});
const SERVICES = { 1: servicesFields(1), 2: servicesFields(2) };

/** Whether a signed services section has exactly the version-1 or version-2 shape and bounds. */
export function validServices(value: unknown): value is ServicesHealth {
  const version = (value as { version?: unknown } | null)?.version;
  return (version === 1 || version === 2) && shaped(value, SERVICES[version]);
}

/** A device-reported UTC time; out-of-range values are shown raw, never thrown on. */
function utcTime(ms: number): string {
  const time = new Date(ms);
  return Number.isNaN(time.getTime()) ? `UTC milliseconds ${ms} (out of range)` : time.toISOString();
}

/** One line per service and per entry, for the device's health details. */
export function describeServices(services: ServicesHealth): string[] {
  const lines: string[] = [];
  const more = (omitted: number) => omitted > 0 ? [`… and ${omitted} more not listed (snapshot size limit)`] : [];
  if (services.messaging) {
    const m = services.messaging;
    lines.push(`Pub/sub: ${m.subscriptions} subscriptions, ${m.queued} queued, ${m.refused} refused, ${m.dropped} dropped`);
    for (const q of m.queues) {
      lines.push(`  ${q.principal}: ${q.queued} queued (${q.fill_percent}% full${q.full_since_ms !== null ? ` since ${utcTime(q.full_since_ms)}` : ""}), `
        + `${q.refused_recent} refused and ${q.dropped_recent} dropped since the previous report`);
    }
    lines.push(...more(m.omitted));
  }
  if (services.bridge) {
    const b = services.bridge;
    lines.push(`MQTT bridge: ${b.connected ? "connected" : "disconnected"} since ${utcTime(b.since_ms)}, ${b.connects} connects, `
      + `${b.refused} refused, ${b.rejected} rejected${b.last_error ? ` (${b.last_error})` : ""}`);
  }
  if (services.streams) {
    for (const st of services.streams.streams) {
      const e = st.export;
      const range = st.oldest === undefined ? "" : st.oldest === null ? " (empty)" : ` (sequences ${st.oldest}–${st.newest})`;
      const checkpoint = e?.checkpoint === undefined ? "" : e.checkpoint === null ? ", nothing exported yet" : `, checkpoint ${e.checkpoint}`;
      lines.push(`Stream ${st.name}: ${st.fill_percent}% of ${st.max_size} bytes, ${st.messages} messages${range}, ${st.rejected} appends rejected`
        + (e ? `; export ${e.lag} behind${checkpoint}, ${e.lost} lost before export, ${e.consecutive_failures} consecutive failures${e.last_error ? ` (${e.last_error})` : ""}` : ""));
    }
    lines.push(...more(services.streams.omitted));
  }
  if (services.shadows) {
    const sh = services.shadows;
    lines.push(`Shadows: ${sh.shadows}, ${sh.notifications_refused} notifications refused`);
    for (const s of sh.sync) {
      lines.push(`  sync ${JSON.stringify(s.name)}: ${s.conflicts} conflicts settled, ${s.consecutive_failures} consecutive failures${s.last_error ? ` (${s.last_error})` : ""}`);
    }
    lines.push(...more(sh.omitted));
  }
  return lines;
}

/** The daemon's signed snapshot: schema 1, or schema 2 with a services section. */
export interface Snapshot {
  service_configuration_sha256?: string;
  schema: number;
  chain_identifier: string;
  registry: string;
  device_id: string;
  signer: string;
  seq: number;
  observed_ms: number;
  interval_seconds: number;
  daemon_version: string;
  boot_id: string | null;
  started_ms: number;
  dropped: number;
  sync: { last_success_ms: number | null; consecutive_failures: number; last_error: string | null };
  pending_receipts: number;
  components: ComponentHealth[];
  health: string;
  reasons: string[];
  services?: ServicesHealth;
}

/** One stored report as the indexer serves it. */
export interface Report {
  device_id: string;
  seq: number;
  signer: string;
  observed_ms: number;
  received_ms: number;
  message: string;
  signature: string;
}

/** Fleet status. `unverified` and `unavailable` are the dApp's own. */
export type FleetStatus = "unreported" | "offline" | "degraded" | "healthy" | "unverified" | "unavailable";

export interface Assessment {
  status: FleetStatus;
  codes: string[];
  reasons: string[];
}

export interface DeviceHealth extends Assessment {
  intervalSeconds?: number;
  lastSeenMs?: number;
  /** Age of the newest report by the indexer's clock (not the browser's). */
  lastSeenAgeMs?: number;
  observedMs?: number;
  daemonVersion?: string;
  components: ComponentHealth[];
  /** From the verified report (SF-085), when the device runs these services. */
  services?: ServicesHealth;
}

/**
 * Status from a device's identity and its newest report from that identity's
 * signer; the same rules and codes as suifleet-indexer (shared cases in
 * interfaces/telemetry-cases.json). Healthy needs a fresh, timely report
 * whose own judgement is healthy and whose facts agree with it.
 */
export function assess(revoked: boolean, latest: { snapshot: Snapshot; receivedMs: number } | undefined, nowMs: number, chainCadenceMs?: number): Assessment {
  if (revoked) {
    return { status: "unreported", codes: ["revoked"], reasons: ["device identity is revoked or deregistered; its telemetry is not trusted"] };
  }
  if (!latest) return { status: "unreported", codes: ["no-telemetry"], reasons: ["no telemetry received"] };
  const { snapshot, receivedMs } = latest;
  const codes: string[] = [];
  const reasons: string[] = [];
  // A chain-configured producer cannot report faster than its enforced cadence.
  // Only the direct-chain reader supplies this independently verified value.
  if (chainCadenceMs !== undefined && (!Number.isSafeInteger(chainCadenceMs) || chainCadenceMs < 60_000 || chainCadenceMs > 86_400_000)) throw new Error("Invalid chain health cadence");
  const interval = chainCadenceMs ?? Math.min(Math.max(snapshot.interval_seconds, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS) * 1000;
  const offline = nowMs - receivedMs > OFFLINE_INTERVALS * interval;
  if (offline) {
    codes.push("offline");
    reasons.push(`no report for ${Math.floor(Math.max(0, nowMs - receivedMs) / 1000)} s`);
  }
  if (receivedMs - snapshot.observed_ms > DELAYED_INTERVALS * interval) {
    codes.push("delayed");
    reasons.push("newest report was taken long before it arrived (buffered offline, or the device clock is behind)");
  }
  if (snapshot.health !== "healthy" || snapshot.reasons.length > 0) {
    codes.push("device-degraded");
    reasons.push(...(snapshot.reasons.length ? snapshot.reasons : ["device reports degraded"]));
  } else if (snapshot.components.some((c) => !c.installed || (c.supervised && c.phase !== "running"))) {
    codes.push("inconsistent");
    reasons.push("device reports healthy, but a component is not installed or not running");
  }
  return { status: offline ? "offline" : codes.length ? "degraded" : "healthy", codes, reasons };
}

/** A device's registry identity (SF-008), read from the chain. */
export interface Identity { signer: string; revoked: boolean }

export async function readIdentity(client: Pick<SuiGrpcClient, "getDynamicField">, deviceId: string): Promise<Identity | undefined> {
  let field;
  try {
    field = await client.getDynamicField({ parentId: requireManifest().shared_objects.registry.id,
      name: { type: moveType("device_registry::IdentityKey"), bcs: bcs.Address.serialize(deviceId).toBytes() } });
  } catch (error) {
    // Only a missing field means "no identity" (an unactivated device).
    if (error instanceof ObjectError && error.reason === "notFound") return undefined;
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("device_registry::Identity"))) {
    throw new Error("Unexpected Identity type");
  }
  const identity = IdentityBcs.parse(field.dynamicField.value.bcs);
  return { signer: normalizeSuiAddress(identity.signer), revoked: identity.revoked };
}

export async function readIdentities(client: Pick<SuiGrpcClient, "getObjects">, deviceIds: string[]): Promise<(Identity | undefined)[]> {
  const values = await batchFields(client, deviceIds.map(device => ({ parent: requireManifest().shared_objects.registry.id,
    keyType: moveType("device_registry::IdentityKey"), key: bcs.Address.serialize(device).toBytes(), valueType: moveType("device_registry::Identity") })));
  return values.map(value => {
    if (!value) return undefined;
    const identity = IdentityBcs.parse(value);
    return { signer: normalizeSuiAddress(identity.signer), revoked: identity.revoked };
  });
}

/**
 * The snapshot of a report, only if its signature verifies for `signer` and
 * it is bound to this deployment and device; otherwise why not.
 */
export async function verifyReport(report: Pick<Report, "message" | "signature">, deviceId: string, signer: string): Promise<Snapshot | string> {
  let bytes: Uint8Array, signature: Uint8Array;
  try {
    bytes = fromBase64(report.message);
    signature = fromBase64(report.signature);
  } catch {
    return "undecodable report";
  }
  // Device keys are plain ed25519 (0x00) or P-256 (0x02) keys.
  if (signature[0] !== 0x00 && signature[0] !== 0x02) return "unsupported signature scheme";
  if (!await isValidPersonalMessageSignature(bytes, report.signature, { address: signer })) {
    return "signature is not the device identity's";
  }
  let snapshot: Snapshot;
  try {
    snapshot = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return "undecodable snapshot";
  }
  const manifest = requireManifest();
  if (snapshot.schema === TELEMETRY_SCHEMA_SERVICES) {
    if (!validServices(snapshot.services)) return "invalid services section";
  } else if (snapshot.schema !== TELEMETRY_SCHEMA || "services" in snapshot) {
    return `unsupported telemetry schema ${snapshot.schema}`;
  }
  if (snapshot.chain_identifier !== manifest.chain_identifier
      || normalizeSuiAddress(snapshot.registry) !== normalizeSuiAddress(manifest.shared_objects.registry.id)) {
    return "report is bound to another deployment";
  }
  if (normalizeSuiAddress(snapshot.device_id) !== normalizeSuiAddress(deviceId)) return "report is for another device";
  if (normalizeSuiAddress(snapshot.signer) !== normalizeSuiAddress(signer)) return "report names another signer";
  return snapshot;
}

/** The indexer's view of one device (GET /v1/health). */
export interface IndexedHealth {
  device_id: string;
  report: Report | null;
}

/** Combine the chain identity with the indexer's newest report. */
export async function deviceHealth(identity: Identity | undefined, indexed: IndexedHealth | undefined, deviceId: string, serverNowMs: number): Promise<DeviceHealth> {
  if (!identity) {
    return { status: "unreported", codes: ["not-activated"], reasons: ["device is not activated; it has no identity to sign telemetry"], components: [] };
  }
  const report = indexed?.report;
  if (!report || identity.revoked) return { ...assess(identity.revoked, undefined, serverNowMs), components: [] };
  const snapshot = await verifyReport(report, deviceId, identity.signer);
  if (typeof snapshot === "string") {
    return { status: "unverified", codes: ["unverified"], reasons: [`newest report rejected: ${snapshot}`], components: [] };
  }
  return {
    ...assess(false, { snapshot, receivedMs: report.received_ms }, serverNowMs),
    lastSeenMs: report.received_ms, lastSeenAgeMs: Math.max(0, serverNowMs - report.received_ms), observedMs: snapshot.observed_ms, daemonVersion: snapshot.daemon_version,
    intervalSeconds: snapshot.interval_seconds,
    components: snapshot.components,
    services: snapshot.services,
  };
}

/** Every device the indexer knows for this operator, with its newest report. */
export async function fetchIndexedHealth(url: string, token: string, owner: string, fetcher: typeof fetch = fetch): Promise<{ items: IndexedHealth[]; serverNowMs: number }> {
  const items: IndexedHealth[] = [];
  let cursor: string | null = null;
  let serverNowMs = 0;
  for (let page = 0; page < 1000; page++) {
    const query = new URLSearchParams({ owner, limit: "500", ...(cursor ? { cursor } : {}) });
    const response = await fetcher(`${url.replace(/\/+$/, "")}/v1/health?${query}`, {
      headers: token ? { authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw new Error(`Telemetry service answered ${response.status}`);
    const body = await response.json() as { items: IndexedHealth[]; next: string | null; server_time_ms: number };
    items.push(...body.items);
    serverNowMs = body.server_time_ms;
    if (!body.next) return { items, serverNowMs };
    if (body.next === cursor) throw new Error("Telemetry pagination made no progress");
    cursor = body.next;
  }
  throw new Error("Telemetry pagination did not end");
}

const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const unavailableHealth = (reason: string): DeviceHealth => ({ status: "unavailable", codes: ["unavailable"], reasons: [reason], components: [] });

/**
 * Health of each of `deviceIds`, or `unavailable` for all when the service
 * cannot answer. A device whose chain identity cannot be read, or whose report
 * cannot be checked, is `unavailable` on its own; this never rejects, so a
 * failed refresh cannot leave an earlier result on screen.
 */
export async function fetchFleetHealth(client: Pick<SuiGrpcClient, "getDynamicField"> & Partial<Pick<SuiGrpcClient, "getObject" | "getObjects" | "ledgerService">>, deviceIds: string[], owner: string,
  url = TELEMETRY_URL, token = "", fetcher: typeof fetch = fetch, scope?: { config?: import("./fleet-data").FleetDataConfig }): Promise<Record<string, DeviceHealth>> {
  const unavailable = (reason: string): Record<string, DeviceHealth> => Object.fromEntries(deviceIds.map((id) => [id, unavailableHealth(reason)]));
  if (scope || !url || process.env.NEXT_PUBLIC_FLEET_DATA) {
    try {
      const { DATA_CONFIG, fetchChainHealth } = await import("./fleet-data");
      const getObject = client.getObject?.bind(client);
      const ledgerService = client.ledgerService;
      const config = scope ? scope.config : DATA_CONFIG;
      if (!config) return unavailable("Configure the on-chain fleet (NEXT_PUBLIC_FLEET_DATA) to read health");
      if (!getObject || !ledgerService) return unavailable("fleet data chain reads unavailable");
      return await fetchChainHealth({ getObject, getDynamicField: client.getDynamicField.bind(client), getObjects: client.getObjects?.bind(client), ledgerService }, config, deviceIds);
    } catch (error) { return unavailable(`fleet data unavailable: ${message(error)}`); }
  }
  let indexed;
  try {
    indexed = await fetchIndexedHealth(url, token, owner, fetcher);
  } catch (error) {
    return unavailable(`telemetry service unavailable: ${message(error)}`);
  }
  const byId = new Map(indexed.items.map((item) => [normalizeSuiAddress(item.device_id), item]));
  let identities: (Identity | undefined)[] | undefined;
  try { if (client.getObjects) identities = await readIdentities({ getObjects: client.getObjects.bind(client) }, deviceIds); }
  catch (error) { return unavailable(`device identities cannot be read from the chain: ${message(error)}`); }
  const entries = await Promise.all(deviceIds.map(async (id, index): Promise<[string, DeviceHealth]> => {
    let identity;
    try {
      identity = identities ? identities[index] : await readIdentity(client, id);
    } catch (error) {
      return [id, unavailableHealth(`device identity cannot be read from the chain: ${message(error)}`)];
    }
    try {
      return [id, await deviceHealth(identity, byId.get(normalizeSuiAddress(id)), id, indexed.serverNowMs)];
    } catch (error) {
      return [id, unavailableHealth(`newest report cannot be checked: ${message(error)}`)];
    }
  }));
  return Object.fromEntries(entries);
}

/** How often the dApp refreshes fleet health. */
export const HEALTH_REFRESH_MS = 60_000;
/** How long a verified result may be shown without a successful refresh. */
export const HEALTH_STALE_MS = 2 * HEALTH_REFRESH_MS;

/**
 * The health to display: the last result only while it is recent and its
 * refresh has not failed. Otherwise every device is `unavailable`, so an
 * outage of the indexer or the chain never freezes an earlier healthy claim.
 */
export function currentHealth(data: Record<string, DeviceHealth> | undefined, updatedAtMs: number, refreshFailed: boolean,
  nowMs: number): Record<string, DeviceHealth> | undefined {
  if (!data) return undefined;
  const ageMs = nowMs - updatedAtMs;
  if (!refreshFailed && ageMs <= HEALTH_STALE_MS) return data;
  const reason = refreshFailed ? "health refresh failed" : `health not refreshed for ${Math.floor(ageMs / 1000)} s`;
  return Object.fromEntries(Object.keys(data).map((id) => [id, unavailableHealth(reason)]));
}

export function lastSeenLabel(health: DeviceHealth | undefined): string {
  if (health?.lastSeenAgeMs === undefined) return "never";
  const seconds = Math.round(health.lastSeenAgeMs / 1000);
  if (seconds < 90) return `${seconds} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}
