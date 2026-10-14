import { bcs } from "@mysten/sui/bcs";

// Exact on-chain layouts; parse BCS rather than transport-dependent JSON shapes.
const id = bcs.Address;
const strings = bcs.struct("StringEntry", { key: bcs.string(), value: bcs.string() });
export const DeviceBcs = bcs.struct("Device", {
  id, name: bcs.string(), device_address: id, arch: bcs.string(),
  meta: bcs.struct("Metadata", { contents: bcs.vector(strings) }),
  registered_at: bcs.u64(), schema_version: bcs.u64(),
});
export const GroupBcs = bcs.struct("DeviceGroup", {
  id, name: bcs.string(), description: bcs.string(), admin: id,
  device_ids: bcs.vector(id), created_at: bcs.u64(), schema_version: bcs.u64(),
});
export const GroupCreatedBcs = bcs.struct("GroupCreated", { group_id: id, name: bcs.string(), admin: id });
export const DeploymentCreatedBcs = bcs.struct("DeploymentCreated", {
  deployment_id: id, component_name: bcs.string(), version: bcs.string(),
  recipe_blob_id: bcs.string(), artifact_sha256: bcs.string(),
  target_groups: bcs.vector(id), target_devices: bcs.vector(id), creator: id,
});
export const DeploymentBcs = bcs.struct("DeploymentRecord", {
  id, creator: id, component_name: bcs.string(), version: bcs.string(),
  recipe_blob_id: bcs.string(), artifact_sha256: bcs.string(),
  created_at: bcs.u64(), schema_version: bcs.u64(),
});
export const ReceiptsBcs = bcs.struct("Receipts", {
  version: bcs.u64(),
  statuses: bcs.struct("Statuses", { contents: bcs.vector(bcs.struct("StatusEntry", { key: id, value: bcs.u8() })) }),
});
export const UpgradeCapBcs = bcs.struct("UpgradeCap", { id, package: id, version: bcs.u64(), policy: bcs.u8() });
