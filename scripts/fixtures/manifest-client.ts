// Browser-safe network fixtures shared by Node and browser wallet acceptance.
import { bcs } from "@mysten/sui/bcs";
import { fromHex, toBase58 } from "@mysten/sui/utils";
import { UpgradeCapBcs } from "../../dapp/src/lib/move-bcs";
import type { DeploymentManifest } from "../../dapp/src/lib/deployment-manifest";

export function manifestClient(m: DeploymentManifest, chain = () => m.chain_identifier) {
  return {
    ledgerService: { getServiceInfo: async () => ({ response: { chainId: toBase58(fromHex(chain() + "00".repeat(28))) } }) },
    getObject: async ({ objectId }: { objectId: string }) => {
      if (objectId === m.call_package_id) return { object: { objectBcs: bcs.Object.serialize({
        data: { Package: { id: m.call_package_id, version: m.package_version, moduleMap: new Map(), linkageTable: new Map(),
          typeOriginTable: Object.entries(m.type_origins).map(([name, origin]) => ({ moduleName: name.split("::")[0], datatypeName: name.split("::")[1], package: origin })) } },
        owner: { Immutable: true }, previousTransaction: toBase58(new Uint8Array(32)), storageRebate: 0,
      }).toBytes() } };
      if (objectId === m.upgrade_cap_id) return { object: { type: "0x2::package::UpgradeCap", content: UpgradeCapBcs.serialize({ id: objectId, package: m.call_package_id, version: m.package_version, policy: 0 }).toBytes() } };
      return { object: { type: `${m.type_origins["device_registry::DeviceRegistry"]}::device_registry::DeviceRegistry`, owner: { $kind: "Shared", Shared: { initialSharedVersion: m.shared_objects.registry.initial_shared_version } } } };
    },
  };
}
