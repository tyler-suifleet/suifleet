import { batchFields } from "./batch-fields";
// SF-082 data package interface v1. Separate package/type origins are explicit:
// upgrading sui_edge does not change the origin of these objects.
import { bcs } from "@mysten/sui/bcs";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { normalizeStructTag, normalizeSuiAddress as id, toBase64, fromBase58, toHex } from "@mysten/sui/utils";
import { requireManifest } from "./deployment-manifest";
import { readIdentities, readIdentity, verifyReport, assess, type DeviceHealth, type Snapshot } from "./telemetry";

const address = bcs.Address;
export const FleetDataBcs = bcs.struct("FleetData", {
  id: address, version: bcs.u64(), budget_id: address, storage_id: address,
  registry_id: address, chain_identifier: bcs.string(), key_epoch: bcs.u64(),
  support: bcs.vector(address), health_interval_ms: bcs.u64(), batch_interval_ms: bcs.u64(),
  emergency_interval_ms: bcs.u64(), device_count: bcs.u64(),
});
export const DeviceDataBcs = bcs.struct("DeviceData", {
  id: address, version: bcs.u64(), fleet_id: address, device_id: address, account_id: address,
  health_seq: bcs.u64(), health_at_ms: bcs.u64(), message: bcs.vector(bcs.u8()), signature: bcs.vector(bcs.u8()),
  batch_seq: bcs.u64(), batch_at_ms: bcs.option(bcs.u64()), emergency_at_ms: bcs.option(bcs.u64()), anchor_count: bcs.u64(),
});
export const AnchorBcs = bcs.struct("Anchor", {
  seq: bcs.u64(), kind: bcs.u8(), blob_object_id: address, blob_id: bcs.u256(),
  ciphertext_sha256: bcs.vector(bcs.u8()), signer: address, at_ms: bcs.u64(), end_epoch: bcs.u32(), key_epoch: bcs.u64(),
});

export interface FleetDataConfig {
  schema: 1;
  chainIdentifier: string;
  registryId: string;
  packageId: string;
  typeOrigin: string;
  fleetId: string;
  budgetId: string;
  storageId: string;
  /** Verified introduction origin of CompactHead/CompactHeadKey, only after upgrade. */
  compactHistoryOrigin?: string;
}

export function parseDataConfig(raw: string): FleetDataConfig {
  const value = JSON.parse(raw);
  const fields = ["schema", "chainIdentifier", "registryId", "packageId", "typeOrigin", "fleetId", "budgetId", "storageId"];
  if (!value || Object.keys(value).filter(key => key !== "compactHistoryOrigin").sort().join() !== fields.sort().join() || value.schema !== 1
    || !/^[0-9a-f]{8}$/.test(value.chainIdentifier)) throw new Error("Invalid fleet data configuration");
  for (const key of fields.filter(k => k !== "schema" && k !== "chainIdentifier")) value[key] = id(value[key]);
  if (value.compactHistoryOrigin !== undefined) value.compactHistoryOrigin = id(value.compactHistoryOrigin);
  const manifest = requireManifest();
  if (value.chainIdentifier !== manifest.chain_identifier || value.registryId !== id(manifest.shared_objects.registry.id)) {
    throw new Error("Fleet data configuration is for another deployment");
  }
  return value;
}

export const DATA_CONFIG = process.env.NEXT_PUBLIC_FLEET_DATA ? parseDataConfig(process.env.NEXT_PUBLIC_FLEET_DATA) : undefined;
type Reads = Pick<SuiGrpcClient, "getObject" | "getDynamicField" | "ledgerService"> & Partial<Pick<SuiGrpcClient, "getObjects">>;
const unavailable = (reason: string): DeviceHealth => ({ status: "unavailable", codes: ["unavailable"], reasons: [reason], components: [] });
const type = (config: FleetDataConfig, name: string) => normalizeStructTag(`${config.typeOrigin}::fleet_data::${name}`);
function safeNumber(value: string): number {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) throw new Error("On-chain integer is outside the browser's safe range");
  return number;
}

export async function readFleetData(client: Reads, config: FleetDataConfig) {
  const { response } = await client.ledgerService.getServiceInfo({});
  const chain = response.chainId ? fromBase58(response.chainId) : new Uint8Array();
  if (chain.length !== 32 || toHex(chain.slice(0, 4)) !== config.chainIdentifier) throw new Error("Fullnode chain identifier mismatch");
  const { object } = await client.getObject({ objectId: config.fleetId, include: { content: true } });
  if (object.owner.$kind !== "Shared" || normalizeStructTag(object.type) !== type(config, "FleetData")) throw new Error("Unexpected FleetData type origin/ownership");
  const fleet = FleetDataBcs.parse(object.content);
  if (fleet.version !== "1" || id(fleet.id) !== config.fleetId || id(fleet.budget_id) !== config.budgetId
    || id(fleet.storage_id) !== config.storageId || id(fleet.registry_id) !== config.registryId
    || fleet.chain_identifier !== config.chainIdentifier) throw new Error("FleetData binding/version mismatch");
  return fleet;
}

export async function readDeviceData(client: Reads, config: FleetDataConfig, deviceId: string) {
  let field;
  try {
    field = await client.getDynamicField({ parentId: config.fleetId,
      name: { type: type(config, "DeviceKey"), bcs: bcs.struct("DeviceKey", { device_id: address }).serialize({ device_id: deviceId }).toBytes() } });
  } catch (error) {
    if (error instanceof ObjectError && error.reason === "notFound") return undefined;
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag("0x2::object::ID")) throw new Error("Unexpected fleet device index type");
  const writerId = address.parse(field.dynamicField.value.bcs);
  const { object } = await client.getObject({ objectId: writerId, include: { content: true } });
  if (object.owner.$kind !== "Shared" || normalizeStructTag(object.type) !== type(config, "DeviceData")) throw new Error("Unexpected DeviceData type origin/ownership");
  const writer = DeviceDataBcs.parse(object.content);
  if (writer.version !== "1" || id(writer.id) !== id(writerId) || id(writer.fleet_id) !== config.fleetId
    || id(writer.device_id) !== id(deviceId)) throw new Error("DeviceData binding/version mismatch");
  return writer;
}

/** Validate fields used to derive health; a signed malformed snapshot is unavailable. */
export function validateHealthSnapshot(snapshot: Snapshot, maxIntervalSeconds = 3600): void {
  const integer = (n: unknown) => typeof n === "number" && Number.isSafeInteger(n) && n >= 0;
  const text = (value: unknown, max: number) => typeof value === "string" && new TextEncoder().encode(value).length <= max;
  const keys = (value: object, allowed: string[]) => Object.keys(value).every(key => allowed.includes(key));
  if (!keys(snapshot, ["schema", "chain_identifier", "registry", "device_id", "signer", "seq", "observed_ms", "interval_seconds",
    "daemon_version", "boot_id", "started_ms", "dropped", "sync", "pending_receipts", "components", "health", "reasons", "services", "service_configuration_sha256"])) {
    throw new Error("Unknown health snapshot field");
  }
  if (![snapshot.seq, snapshot.observed_ms, snapshot.started_ms, snapshot.dropped, snapshot.pending_receipts].every(integer)
    || !integer(snapshot.interval_seconds) || snapshot.interval_seconds < 10 || snapshot.interval_seconds > maxIntervalSeconds
    || !snapshot.sync || !integer(snapshot.sync.consecutive_failures)
    || !keys(snapshot.sync, ["last_success_ms", "consecutive_failures", "last_error"])
    || !(snapshot.sync.last_success_ms === null || integer(snapshot.sync.last_success_ms))
    || !(snapshot.sync.last_error === null || text(snapshot.sync.last_error, 200))
    || !["healthy", "degraded"].includes(snapshot.health)
    || !text(snapshot.daemon_version, 128) || !(snapshot.boot_id === null || text(snapshot.boot_id, 64))
    || (snapshot.service_configuration_sha256 !== undefined && !/^[0-9a-f]{64}$/.test(snapshot.service_configuration_sha256))
    || !Array.isArray(snapshot.reasons) || snapshot.reasons.length > 16
    || !snapshot.reasons.every(r => text(r, 200))
    || !Array.isArray(snapshot.components) || snapshot.components.length > 64) throw new Error("Invalid health snapshot shape");
  for (const component of snapshot.components) {
    if (!component || !keys(component, ["name", "version", "deployment_id", "installed", "supervised", "phase", "restarts", "last_exit", "since_ms"])
      || !text(component.name, 128) || !text(component.version, 128) || !text(component.deployment_id, 128)
      || typeof component.installed !== "boolean" || typeof component.supervised !== "boolean"
      || !(component.phase === null || text(component.phase, 128))
      || !(component.restarts === null || integer(component.restarts))
      || !(component.since_ms === null || integer(component.since_ms))
      || !(component.last_exit === null || text(component.last_exit, 200))) throw new Error("Invalid health component shape");
  }
}

/** Reads chain objects directly. A configured data path never silently falls back to an indexer. */
export async function fetchChainHealth(client: Reads, config: FleetDataConfig, deviceIds: string[], now = Date.now()): Promise<Record<string, DeviceHealth>> {
  let cadenceMs: number;
  try { cadenceMs = safeNumber((await readFleetData(client, config)).health_interval_ms);
    if (cadenceMs < 60_000 || cadenceMs > 86_400_000) throw new Error("Invalid chain health cadence"); }
  catch (error) { return Object.fromEntries(deviceIds.map(device => [device, unavailable(String(error))])); }
  let identities: Awaited<ReturnType<typeof readIdentities>> | undefined;
  let writers: (ReturnType<typeof DeviceDataBcs.parse> | undefined)[] | undefined;
  try {
    if (client.getObjects) {
      const batch = { getObjects: client.getObjects.bind(client) };
      identities = await readIdentities(batch, deviceIds);
      const values = await batchFields(batch, deviceIds.map(device => ({ parent: config.fleetId,
        keyType: type(config, "DeviceKey"), key: address.serialize(device).toBytes(), valueType: "0x2::object::ID" })));
      const writerIds = values.map(value => value ? address.parse(value) : undefined);
      const ids = writerIds.filter((value): value is string => !!value);
      const byId = new Map<string, ReturnType<typeof DeviceDataBcs.parse>>();
      for (let offset = 0; offset < ids.length; offset += 50) {
        const { objects } = await batch.getObjects({ objectIds: ids.slice(offset, offset + 50), include: { content: true } });
        if (objects.length !== Math.min(50, ids.length - offset)) throw Error("Incomplete health batch");
        objects.forEach((object, index) => {
          if (object instanceof Error) throw object;
          if (object.owner.$kind !== "Shared" || normalizeStructTag(object.type) !== type(config, "DeviceData") || id(object.objectId) !== id(ids[offset + index])) throw Error("Unexpected DeviceData type origin/ownership/ID");
          const writer = DeviceDataBcs.parse(object.content);
          if (writer.version !== "1" || id(writer.id) !== id(object.objectId) || id(writer.fleet_id) !== config.fleetId) throw Error("DeviceData binding/version mismatch");
          byId.set(id(object.objectId), writer);
        });
      }
      writers = writerIds.map((writerId, index) => {
        if (!writerId) return undefined;
        const writer = byId.get(id(writerId))!;
        if (id(writer.device_id) !== id(deviceIds[index])) throw Error("DeviceData device mismatch");
        return writer;
      });
    }
  } catch (error) { return Object.fromEntries(deviceIds.map(device => [device, unavailable(`Chain health batch could not be verified: ${String(error)}`)])); }
  return Object.fromEntries(await Promise.all(deviceIds.map(async (deviceId, index) => {
    try {
      const identity = identities ? identities[index] : await readIdentity(client, deviceId);
      if (!identity || identity.revoked) return [deviceId, { ...assess(!!identity?.revoked, undefined, now), components: [] }];
      const writer = writers ? writers[index] : await readDeviceData(client, config, deviceId);
      if (!writer || writer.health_seq === "0") return [deviceId, { ...assess(false, undefined, now), components: [] }];
      if (writer.message.length > 16_384) throw new Error("Health snapshot exceeds its chain bound");
      const snapshot = await verifyReport({ message: toBase64(new Uint8Array(writer.message)), signature: toBase64(new Uint8Array(writer.signature)) }, deviceId, identity.signer);
      if (typeof snapshot === "string") throw new Error(snapshot);
      validateHealthSnapshot(snapshot, 86_400);
      if (BigInt(snapshot.seq) !== BigInt(writer.health_seq)) throw new Error("Snapshot sequence differs from the chain anchor");
      const receivedMs = safeNumber(writer.health_at_ms);
      if (receivedMs > now + 300_000 || snapshot.observed_ms > receivedMs + 300_000) throw new Error("Health timestamp is in the future");
      return [deviceId, { ...assess(false, { snapshot, receivedMs }, now, cadenceMs), lastSeenMs: receivedMs,
        lastSeenAgeMs: Math.max(0, now - receivedMs), observedMs: snapshot.observed_ms,
        daemonVersion: snapshot.daemon_version, intervalSeconds: snapshot.interval_seconds, components: snapshot.components, services: snapshot.services }];
    } catch (error) { return [deviceId, unavailable(`Chain health could not be verified: ${String(error)}`)]; }
  })));
}

/** Public fleet index, paged directly from Sui. Read/decrypt authorization is
 * checked separately; index visibility never grants access to ciphertext keys. */
export async function listFleetDeviceIds(client: SuiGrpcClient, config: FleetDataConfig, cursor?: string | null) {
  await readFleetData(client, config);
  const page = await client.listDynamicFields({ parentId: config.fleetId, cursor, limit: 100 });
  if (page.dynamicFields.length > 100 || (page.hasNextPage && (!page.cursor || page.cursor === cursor))) throw Error('Invalid fleet index page');
  const devices: string[] = [];
  for (const field of page.dynamicFields) {
    if (normalizeStructTag(field.name.type) !== type(config, 'DeviceKey')) continue;
    if (field.$kind !== 'DynamicField' || normalizeStructTag(field.valueType) !== normalizeStructTag('0x2::object::ID') || field.name.bcs.length !== 32) throw Error('Invalid fleet device index');
    const device = id(address.parse(field.name.bcs));
    if (devices.includes(device)) throw Error('Duplicate fleet device index');
    devices.push(device);
  }
  return { devices, cursor: page.cursor, hasNextPage: page.hasNextPage };
}
