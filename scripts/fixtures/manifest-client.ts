// Browser-safe network fixtures shared by Node and browser wallet acceptance.
import { bcs } from "@mysten/sui/bcs";
import { fromHex, toBase58 } from "@mysten/sui/utils";
import { UpgradeCapBcs } from "../../dapp/src/lib/move-bcs";
import type { DeploymentManifest } from "../../dapp/src/lib/deployment-manifest";

export function manifestClient(m: DeploymentManifest, chain = () => m.chain_identifier) {
  const client = {
    ledgerService: {
      getServiceInfo: async () => ({ response: { chainId: toBase58(fromHex(chain() + "00".repeat(28))), lowestAvailableCheckpoint: BigInt(0) } }),
      // Bridge legacy query fixtures to actual streaming gRPC frames. Tests of
      // retention/cursor behavior exercise the raw service separately.
      listEvents: (request: { filter?: { terms: { literals: { predicate: { oneofKind?: string; eventType?: { eventType?: string }; sender?: { address?: string } } }[] }[] }; options?: { after?: Uint8Array } }) => ({ responses: (async function* () {
        const literals = request.filter?.terms[0].literals ?? [];
        const filter = { eventType: literals.find(l => l.predicate.oneofKind === 'eventType')?.predicate.eventType?.eventType,
          sender: literals.find(l => l.predicate.oneofKind === 'sender')?.predicate.sender?.address };
        const before = request.options?.after ? new TextDecoder().decode(request.options.after) : undefined;
        const page = await client.listEvents({ filter, before });
        let index = 0;
        for (const event of [...page.events].reverse()) yield { event: { ...event, contents: { value: event.bcs }, checkpoint: BigInt(0),
          transactionDigest: event.transactionDigest ?? toBase58(new Uint8Array(32)), eventIndex: event.eventIndex ?? index++ },
          watermark: { cursor: new TextEncoder().encode(page.endCursor ?? 'tip'), checkpoint: BigInt(0) } };
        yield { watermark: { cursor: new TextEncoder().encode(page.endCursor ?? 'tip'), checkpoint: BigInt(0) }, end: { reason: page.hasNextPage ? 2 : 5 } };
      })() }),
    },
    listEvents: async (_request: unknown): Promise<{ events: { bcs: Uint8Array; transactionDigest?: string; eventIndex?: number }[]; hasNextPage: boolean; endCursor?: string }> => ({ events: [], hasNextPage: false }),
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
  return client;
}
