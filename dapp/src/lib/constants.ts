import { MANIFEST, moveType } from "./deployment-manifest";

export const PACKAGE_ID = MANIFEST?.call_package_id ?? "";
export const REGISTRY_ID = MANIFEST?.shared_objects.registry.id ?? "";
export const NETWORK = MANIFEST?.network ?? "testnet";
export const RPC_URL = MANIFEST?.rpc_url;
export const WALRUS_PUBLISHER = MANIFEST?.walrus.publisher_url ?? "";
export const WALRUS_AGGREGATOR = MANIFEST?.walrus.aggregator_url ?? "";
export const DEVICE_TYPE = MANIFEST ? moveType("device_registry::Device") : "";
export const DEVICE_GROUP_TYPE = MANIFEST ? moveType("device_group::DeviceGroup") : "";
export const DEPLOYMENT_CREATED_EVENT = MANIFEST ? moveType("deployment_manager::DeploymentCreated") : "";
export const STATUS_UPDATED_EVENT = MANIFEST ? moveType("deployment_manager::DeviceStatusUpdated") : "";
// Storage period for new uploads, in Walrus epochs; devices cannot download after it ends.
export const WALRUS_EPOCHS = Math.max(1, Number(process.env.NEXT_PUBLIC_WALRUS_EPOCHS ?? 5) || 5);
// Optional per-upload storage budget in FROST (1 WAL = 1e9 FROST); exceeding it raises a warning.
export const WALRUS_MAX_COST_FROST = process.env.NEXT_PUBLIC_WALRUS_MAX_COST_FROST
  ? BigInt(process.env.NEXT_PUBLIC_WALRUS_MAX_COST_FROST)
  : null;


export const STATUS_LABELS: Record<number, string> = {
  0: "Pending",
  1: "Applied",
  2: "Failed",
};

export const STATUS_COLORS: Record<number, string> = {
  0: "bg-yellow-100 text-yellow-800",
  1: "bg-green-100 text-green-800",
  2: "bg-red-100 text-red-800",
};
