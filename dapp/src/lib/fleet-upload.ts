// Fleet-paid Walrus uploads (SF-083): the owner's artifacts and recipes are
// registered from the fleet's prepaid storage and WAL through
// fleet_storage::register_fleet_blob, so the blob is fleet-owned and
// non-deletable like device uploads. There is no publisher and no server:
// this browser encodes the blob with the pinned Walrus SDK, the owner's
// wallet registers it, the slivers go straight to the storage nodes, and the
// wallet certifies it with the nodes' certificate. See docs/FLEET_BUDGET.md.
import { bcs } from "@mysten/sui/bcs";
import { requireWeightedQuorum } from "../../scripts/walrus-node-writes.mjs";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import type { StorageConfig } from "./fleet-budget";
import type { StorageInfo, UploadObserver } from "./walrus";

const CLOCK = "0x6";
/** Walrus encoding type RS2, the only one the pinned SDK encodes. */
export const RS2 = 1;

export interface FleetStorageTarget {
  config: StorageConfig;
  storageId: string;
  budgetId: string;
  capId: string;
  managed?: { customerId: string; fleetId: string };
  retention?: { target: number; maxFee: bigint };
}

export interface ExecutedEvent {
  eventType: string;
  bcs: Uint8Array;
}

/** Sign, execute and confirm one transaction; returns its digest and events (useFleetTransaction). */
export type Execute = (transaction: Transaction) => Promise<{ digest: string; events: ExecutedEvent[] }>;

/** Bitmap of the committee members whose confirmations the certificate aggregates. */
export function signersToBitmap(signers: number[], committeeSize: number): Uint8Array {
  const bitmap = new Uint8Array(Math.ceil(committeeSize / 8));
  for (const signer of signers) {
    if (!Number.isInteger(signer) || signer < 0 || signer >= committeeSize) throw new Error(`Signer ${signer} is not in the committee`);
    bitmap[Math.floor(signer / 8)] |= 1 << (signer % 8);
  }
  return bitmap;
}

export function buildRegisterFleetBlobTx(target: FleetStorageTarget, blobId: bigint, rootHash: bigint, size: number): Transaction {
  if (!Number.isSafeInteger(size) || size <= 0) throw new Error("Upload a nonempty blob");
  const tx = new Transaction();
  if (target.retention && (!Number.isInteger(target.retention.target) || target.retention.target <= 0 || target.retention.maxFee <= BigInt(0))) throw new Error("Choose a finite renewal target and positive maximum WAL quote before uploading");
  tx.moveCall({ target: `${target.config.packageId}::fleet_storage::${target.managed ? (target.retention ? 'register_managed_retained_fleet_blob' : 'register_managed_fleet_blob') : (target.retention ? 'register_retained_fleet_blob' : 'register_fleet_blob')}`, arguments: [...(target.managed ? [tx.object(target.managed.customerId), tx.object(target.managed.fleetId)] : []), tx.object(target.storageId), tx.object(target.budgetId),
    tx.object(target.capId), tx.object(target.config.walrus.system), tx.pure.u256(blobId), tx.pure.u256(rootHash), tx.pure.u64(size), tx.pure.u8(RS2), tx.object(CLOCK),
    ...(target.retention ? [tx.pure.u32(target.retention.target), tx.pure.u64(target.retention.maxFee)] : [])] });
  return tx;
}

export function buildCertifyFleetBlobTx(target: FleetStorageTarget, blobObjectId: string,
  certificate: { signature: Uint8Array; signers: number[]; serializedMessage: Uint8Array }, committeeSize: number): Transaction {
  const tx = new Transaction();
  tx.moveCall({ target: `${target.config.packageId}::fleet_storage::certify_blob`, arguments: [tx.object(target.storageId), tx.object(target.config.walrus.system),
    tx.pure.id(blobObjectId), tx.pure.vector("u8", certificate.signature), tx.pure.vector("u8", signersToBitmap(certificate.signers, committeeSize)),
    tx.pure.vector("u8", certificate.serializedMessage)] });
  return tx;
}

const FleetUploadRegisteredBcs = bcs.struct("FleetUploadRegistered", { storage_id: bcs.Address, blob_object_id: bcs.Address, blob_id: bcs.u256(),
  size: bcs.u64(), encoded_size: bcs.u64(), write_fee: bcs.u64(), end_epoch: bcs.u32(), pool_size: bcs.u64(), wal_balance: bcs.u64() });

/** The registration's FleetUploadRegistered event for this storage and blob, from the confirmed effects. */
export function registeredBlob(events: ExecutedEvent[], target: FleetStorageTarget, blobId: bigint) {
  const type = normalizeStructTag(`${target.config.storageOrigin ?? target.config.packageId}::fleet_storage::FleetUploadRegistered`);
  for (const event of events) {
    if (normalizeStructTag(event.eventType) !== type) continue;
    const value = FleetUploadRegisteredBcs.parse(event.bcs);
    if (normalizeSuiAddress(value.storage_id) !== normalizeSuiAddress(target.storageId) || BigInt(value.blob_id) !== blobId) continue;
    return { blobObjectId: normalizeSuiAddress(value.blob_object_id), endEpoch: value.end_epoch, writeFee: BigInt(value.write_fee), encodedSize: BigInt(value.encoded_size) };
  }
  throw new Error("The registration confirmed without a fleet upload event for this blob; nothing was written to the storage nodes");
}

/**
 * Store `body` in the fleet's prepaid Walrus storage. Each step is checked:
 * the live Walrus objects must be the pinned ones, the registration's
 * confirmed effects name the blob, and certification must confirm. A failure
 * before certification leaves an uncertified fleet blob that the owner can
 * release from the budget page; it never falls back to the publisher.
 */
export async function uploadToFleetStorage(client: SuiGrpcClient, target: FleetStorageTarget, body: Uint8Array, execute: Execute,
  progress?: UploadObserver): Promise<StorageInfo> {
  progress?.({ phase: "preparing", loaded: 0, total: body.byteLength });
  const { walrus, blobIdToInt, TESTNET_WALRUS_PACKAGE_CONFIG } = await import("@mysten/walrus");
  const pinned = target.config.walrus;
  if (normalizeSuiAddress(TESTNET_WALRUS_PACKAGE_CONFIG.systemObjectId) !== normalizeSuiAddress(pinned.system)) throw new Error("The Walrus SDK names another system object than the pinned one");
  const walrusClient = client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus;
  const system = await walrusClient.systemObject();
  if (normalizeSuiAddress(system.package_id) !== normalizeSuiAddress(pinned.walrusPackage)) {
    throw new Error(`The live Walrus package ${system.package_id} is not the pinned ${pinned.walrusPackage} fleet storage was built against`);
  }
  const encoded = await walrusClient.encodeBlob(body);
  const blobId = blobIdToInt(encoded.blobId);
  const rootHash = BigInt(bcs.u256().parse(encoded.rootHash));
  const registered = await execute(buildRegisterFleetBlobTx(target, blobId, rootHash, body.byteLength));
  const blob = registeredBlob(registered.events, target, blobId);
  progress?.({ phase: "uploading", loaded: 0, total: body.byteLength });
  const confirmations = await walrusClient.writeEncodedBlobToNodes({ blobId: encoded.blobId, metadata: encoded.metadata,
    sliversByNode: encoded.sliversByNode, deletable: false, objectId: blob.blobObjectId });
  progress?.({ phase: "processing", loaded: body.byteLength, total: body.byteLength });
  const certificate = await walrusClient.certificateFromConfirmations({ confirmations, blobId: encoded.blobId, deletable: false, blobObjectId: blob.blobObjectId });
  const committee = (await walrusClient.systemState()).committee;
  requireWeightedQuorum(committee, certificate.signers);
  await execute(buildCertifyFleetBlobTx(target, blob.blobObjectId, certificate, committee.members.length));
  progress?.({ phase: "complete", loaded: body.byteLength, total: body.byteLength });
  return { blobId: encoded.blobId, blobObjectId: blob.blobObjectId, endEpoch: blob.endEpoch, costFrost: blob.writeFee, owner: "fleet", alreadyCertified: false };
}
