import clientPackage from "../../package.json";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { bcs } from "@mysten/sui/bcs";
import { fromBase58, toHex, normalizeStructTag } from "@mysten/sui/utils";
import { UpgradeCapBcs } from "./move-bcs";

export const REQUIRED_TYPES = [
  "device_registry::DeviceRegistry", "device_registry::Device",
  "device_group::DeviceGroup", "device_group::GroupCreated",
  "device_cap::DeviceCap", "device_cap::DeviceAddressKey",
  "deployment_manager::DeploymentRecord", "deployment_manager::DeploymentCreated",
  "deployment_manager::DeviceStatusUpdated", "deployment_manager::TargetGroupsKey",
  "deployment_manager::TargetDevicesKey", "deployment_manager::StatusesKey",
  "deployment_manager::RecipeDigestKey", "deployment_manager::ReceiptsKey",
  "deployment_manager::Receipts", "deployment_manager::DeviceStatusReported",
  "device_registry::IdentityKey", "device_registry::Identity", "device_registry::DeviceIdentityChanged",
] as const;
export interface DeploymentManifest {
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
export function parseManifest(raw: string): DeploymentManifest {
  const m: DeploymentManifest = JSON.parse(raw);
  if (m.schema_version !== 1 || m.protocol_version !== 1) fail("unsupported schema/protocol");
  if (!["localnet", "devnet", "testnet", "mainnet"].includes(m.network)) fail("unsupported network");
  if ((m.network === "mainnet" && m.chain_identifier !== "35834a8a") || (m.network === "testnet" && m.chain_identifier !== "4c78adac")) fail("network label/chain mismatch");
  if (!/^[0-9a-f]{8}$/.test(m.chain_identifier)) fail("invalid chain identifier");
  if (!/^[1-9][0-9]*$/.test(m.package_version)) fail("invalid package version");
  normalizeId(m.call_package_id);
  normalizeId(m.upgrade_cap_id);
  normalizeId(m.shared_objects.registry.id);
  if (!/^[1-9][0-9]*$/.test(m.shared_objects.registry.initial_shared_version)) fail("invalid shared version");
  for (const name of REQUIRED_TYPES) normalizeId(m.type_origins[name]);
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
export function moveType(name: typeof REQUIRED_TYPES[number]): string {
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
  for (const name of REQUIRED_TYPES) {
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
