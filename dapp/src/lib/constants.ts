// Populated by scripts/redeploy.sh into .env.local
export const PACKAGE_ID = process.env.NEXT_PUBLIC_PACKAGE_ID ?? "";
// Original (first-ever) package ID — SUI preserves type origins across upgrades,
// so object types and event types always use this ID even after upgrades.
export const ORIGINAL_PACKAGE_ID = process.env.NEXT_PUBLIC_ORIGINAL_PACKAGE_ID || PACKAGE_ID;
export const REGISTRY_ID = process.env.NEXT_PUBLIC_REGISTRY_ID ?? "";

export const NETWORK = (process.env.NEXT_PUBLIC_NETWORK ?? "devnet") as "devnet" | "testnet" | "mainnet";

// Walrus endpoints
export const WALRUS_PUBLISHER = "https://publisher.walrus-testnet.walrus.space";
export const WALRUS_AGGREGATOR = "https://aggregator.walrus-testnet.walrus.space";

// Type queries use ORIGINAL_PACKAGE_ID; function calls use PACKAGE_ID.
export const DEVICE_TYPE = `${ORIGINAL_PACKAGE_ID}::device_registry::Device`;
export const DEVICE_GROUP_TYPE = `${ORIGINAL_PACKAGE_ID}::device_group::DeviceGroup`;
export const DEPLOYMENT_CREATED_EVENT = `${ORIGINAL_PACKAGE_ID}::deployment_manager::DeploymentCreated`;
export const STATUS_UPDATED_EVENT = `${ORIGINAL_PACKAGE_ID}::deployment_manager::DeviceStatusUpdated`;

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
