import { bcs } from "@mysten/sui/bcs";
export { device_registry_DeviceBcs as DeviceBcs, device_registry_DeviceOwnerCapBcs as DeviceOwnerCapBcs, device_group_DeviceGroupBcs as GroupBcs, device_group_GroupCreatedBcs as GroupCreatedBcs, deployment_manager_DeploymentCreatedBcs as DeploymentCreatedBcs, deployment_manager_DeploymentRecordBcs as DeploymentBcs, deployment_manager_ReceiptsBcs as ReceiptsBcs } from "./fleet-protocol";
const id = bcs.Address;
export const UpgradeCapBcs = bcs.struct("UpgradeCap", { id, package: id, version: bcs.u64(), policy: bcs.u8() });
