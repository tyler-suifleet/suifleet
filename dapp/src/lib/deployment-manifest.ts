import clientPackage from "../../package.json";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { bcs } from "@mysten/sui/bcs";
import { fromBase58, toHex, normalizeStructTag } from "@mysten/sui/utils";
import { UpgradeCapBcs } from "./move-bcs";

import { REQUIRED_TYPES, PROTOCOL_VERSION, MANIFEST_SCHEMA_VERSION } from "./protocol-version";
export { REQUIRED_TYPES } from "./protocol-version";
import { BUDGET_TYPES, CATALOG_TYPES, GROUP_BATCH_TYPES, HANDOVER_TYPES, SEAL_POLICY_TYPES, ROLLOUT_HEALTH_TYPES, SETTINGS_TYPES, INBOX_TYPES, CUSTOMER_TYPES, SHARD_TYPES } from "./protocol-version";
export { BUDGET_TYPES, CATALOG_TYPES, SEAL_POLICY_TYPES, ROLLOUT_HEALTH_TYPES, SETTINGS_TYPES, INBOX_TYPES, CUSTOMER_TYPES, SHARD_TYPES } from "./protocol-version";
function manifestTypes(m: DeploymentManifest): string[] {
  const types: string[] = [...REQUIRED_TYPES];
  for (const [label, group] of [["catalog", CATALOG_TYPES], ["seal policy", SEAL_POLICY_TYPES], ["fleet budget", BUDGET_TYPES], ["fleet settings", SETTINGS_TYPES], ["rollout health", ROLLOUT_HEALTH_TYPES], ["fleet inbox", INBOX_TYPES], ["customer", CUSTOMER_TYPES], ["receipt shards", SHARD_TYPES]] as const) {
    const present = group.filter(name => name in m.type_origins);
    if (present.length && present.length !== group.length) fail(`partial ${label} type origins`);
    types.push(...present);
  }
  return [...types, ...[...HANDOVER_TYPES, ...GROUP_BATCH_TYPES].filter(name => name in m.type_origins)];
}
export interface DeploymentManifest {
  // Optional for legacy nonpayment deployments; payments require a pinned payee.
  treasury_payee?: string;
  schema_version: number;
  protocol_version: number;
  network: "localnet" | "devnet" | "testnet" | "mainnet";
  chain_identifier: string;
  rpc_url: string;
  call_package_id: string;
  upgrade_cap_id: string;
  package_version: string;
  type_origins: Record<string, string>;
  shared_objects: { registry: { id: string; initial_shared_version: string } };
  supported_clients: Record<string, { min: string; max_exclusive: string }>;
  walrus: { publisher_url: string; aggregator_url: string };
}
function fail(message: string): never { throw new Error(`Deployment manifest: ${message}`); }
export function normalizeId(value: string): string {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]{1,64}$/.test(value)) fail("invalid Sui ID");
  return `0x${value.slice(2).toLowerCase().padStart(64, "0")}`;
}
function version(value: string): number[] {
  if (typeof value !== "string" || !/^[0-9]+\.[0-9]+\.[0-9]+$/.test(value)) fail("invalid client version");
  return value.split(".").map(Number);
}
function compare(a: number[], b: number[]): number {
  for (let i = 0; i < 3; i++) { if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1; }
  return 0;
}
function endpoint(value: string) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || !url.hostname || url.username || url.password || url.hash) fail("invalid public endpoint");
}
function validateTreasuryPayee(value: unknown): string {
  if (typeof value !== "string" || !/^0x[0-9a-f]{64}$/.test(value) || BigInt(value) === BigInt(0))
    fail("treasury payee mismatch: missing or malformed pinned address; payment blocked");
  return value;
}
// Payment and card-checkout callers must use this guard before signing/redirecting.
// A SuiNS lookup, wallet account or provider response never supplies the payee.
export function treasuryPayee(expectedPayee?: string): string {
  const payee = validateTreasuryPayee(requireManifest().treasury_payee);
  if (expectedPayee !== undefined && expectedPayee !== payee)
    fail("treasury payee mismatch: payment recipient differs from manifest; payment blocked");
  return payee;
}
export function parseManifest(raw: string): DeploymentManifest {
  const m: DeploymentManifest = JSON.parse(raw);
  if (m.schema_version !== MANIFEST_SCHEMA_VERSION || m.protocol_version !== PROTOCOL_VERSION) fail("unsupported schema/protocol");
  if (!["localnet", "devnet", "testnet", "mainnet"].includes(m.network)) fail("unsupported network");
  if ((m.network === "mainnet" && m.chain_identifier !== "35834a8a") || (m.network === "testnet" && m.chain_identifier !== "4c78adac")) fail("network label/chain mismatch");
  if (!/^[0-9a-f]{8}$/.test(m.chain_identifier)) fail("invalid chain identifier");
  if (typeof m.package_version !== "string" || !/^[1-9][0-9]*$/.test(m.package_version)) fail("invalid package version");
  if ("treasury_payee" in m) validateTreasuryPayee(m.treasury_payee);
  normalizeId(m.call_package_id);
  normalizeId(m.upgrade_cap_id);
  normalizeId(m.shared_objects.registry.id);
  if (typeof m.shared_objects.registry.initial_shared_version !== "string" || !/^[1-9][0-9]*$/.test(m.shared_objects.registry.initial_shared_version)) fail("invalid shared version");
  for (const name of manifestTypes(m)) normalizeId(m.type_origins[name]);
  const range = m.supported_clients.dapp;
  const current = version(clientPackage.version);
  if (compare(version(range.min), current) > 0 || compare(current, version(range.max_exclusive)) >= 0) fail("unsupported dapp client version");
  endpoint(m.rpc_url); endpoint(m.walrus.publisher_url); endpoint(m.walrus.aggregator_url);
  return m;
}
// Absence permits an unconfigured static build, but never a query or transaction.
export const MANIFEST = process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST
  ? parseManifest(process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST) : undefined;
export function requireManifest(): DeploymentManifest {
  return MANIFEST ?? fail("not configured; generate frontend.env from a verified manifest");
}
export function moveType(name: typeof REQUIRED_TYPES[number] | typeof CATALOG_TYPES[number] | typeof SEAL_POLICY_TYPES[number] | typeof HANDOVER_TYPES[number] | typeof BUDGET_TYPES[number] | typeof SETTINGS_TYPES[number] | typeof ROLLOUT_HEALTH_TYPES[number] | typeof INBOX_TYPES[number] | typeof CUSTOMER_TYPES[number] | typeof SHARD_TYPES[number] | typeof GROUP_BATCH_TYPES[number]): string {
  const m = requireManifest();
  return `${normalizeId(m.type_origins[name])}::${name}`;
}
// Recheck before each operation: cached network proof cannot detect a devnet reset.
export async function verifyDeployment(client: SuiGrpcClient, m = requireManifest()): Promise<void> {
  // SDK getChainIdentifier caches service info. Read it directly every time so
  // a local/devnet reset cannot reuse an earlier proof.
  const { response } = await client.ledgerService.getServiceInfo({});
  if (!response.chainId) fail("missing chain identifier");
  const digest = fromBase58(response.chainId);
  if (digest.length !== 32 || toHex(digest.slice(0, 4)) !== m.chain_identifier) fail("chain identifier mismatch; network may have reset");
  const registry = m.shared_objects.registry;
  const [{ object: packageObject }, { object: cap }, { object }] = await Promise.all([
    client.getObject({ objectId: m.call_package_id, include: { objectBcs: true } }),
    client.getObject({ objectId: m.upgrade_cap_id, include: { content: true } }),
    client.getObject({ objectId: registry.id }),
  ]);
  const raw = bcs.Object.parse(packageObject.objectBcs).data;
  if (!raw.Package || raw.Package.version !== m.package_version || normalizeId(raw.Package.id) !== normalizeId(m.call_package_id)) fail("package metadata mismatch");
  for (const name of manifestTypes(m)) {
    const [module, datatype] = name.split("::");
    const entries = raw.Package.typeOriginTable.filter((entry) => entry.moduleName === module && entry.datatypeName === datatype);
    if (entries.length !== 1 || normalizeId(entries[0].package) !== normalizeId(m.type_origins[name])) fail(`type origin mismatch: ${name}`);
  }
  if (normalizeStructTag(cap.type) !== normalizeStructTag("0x2::package::UpgradeCap")) fail("invalid UpgradeCap type");
  const capFields = UpgradeCapBcs.parse(cap.content);
  if (normalizeId(capFields.package) !== normalizeId(m.call_package_id) || capFields.version !== m.package_version) fail("UpgradeCap does not identify latest call package/version");
  const expected = `${normalizeId(m.type_origins["device_registry::DeviceRegistry"])}::device_registry::DeviceRegistry`;
  if (normalizeStructTag(object.type) !== expected || object.owner.$kind !== "Shared" || object.owner.Shared.initialSharedVersion !== registry.initial_shared_version) fail("registry type/shared version mismatch");
}
