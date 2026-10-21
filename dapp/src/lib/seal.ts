// Confidential delivery (SF-022): Seal encryption in the browser, before any
// publisher/relay/Walrus upload. The blob uploaded is MAGIC || Seal
// EncryptedObject; devices decrypt it with their own enrolled identity
// (docs/CONFIDENTIAL_DELIVERY.md). Public metadata: the identity (access
// policy, key epoch, random nonce), key server IDs, threshold, and the AAD
// naming the blob kind and plaintext SHA-256, which is also on chain.
import { DemType, EncryptedObject, SealClient } from "@mysten/seal";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { fromHex, toHex } from "@mysten/sui/utils";
import { normalizeId } from "./deployment-manifest";

export const MAGIC = new TextEncoder().encode("suifleet/seal/v1\n");
export type BlobKind = "artifact" | "recipe";

export interface KeyServerSetting { objectId: string; weight: number }
export interface SealSettings { keyServers: KeyServerSetting[]; threshold: number }

/**
 * Key server object IDs, weights and the threshold. These are public; a
 * provider API key never belongs in a frontend bundle, so none is accepted.
 */
export function parseSealSettings(servers: string | undefined, threshold: string | undefined): SealSettings | undefined {
  if (!servers) return undefined;
  const parsed: unknown = JSON.parse(servers);
  if (!Array.isArray(parsed) || parsed.length === 0) throw new Error("Seal key servers: expected a nonempty JSON array");
  const keyServers = parsed.map((entry: Record<string, unknown>) => {
    const extra = Object.keys(entry).filter(key => key !== "objectId" && key !== "weight");
    if (extra.length) throw new Error(`Seal key servers: unexpected ${extra.join(", ")} (provider credentials never belong in the frontend)`);
    const weight = Number(entry.weight ?? 1);
    if (!Number.isInteger(weight) || weight < 1 || weight > 255) throw new Error("Seal key servers: invalid weight");
    return { objectId: normalizeId(String(entry.objectId)), weight };
  });
  if (new Set(keyServers.map(s => s.objectId)).size !== keyServers.length) throw new Error("Seal key servers: duplicate object ID");
  const total = keyServers.reduce((sum, s) => sum + s.weight, 0);
  const t = Number(threshold ?? Math.min(2, total));
  if (!Number.isInteger(t) || t < 1 || t > total) throw new Error("Seal threshold must be between 1 and the total key server weight");
  return { keyServers, threshold: t };
}

export const SEAL_SETTINGS = parseSealSettings(process.env.NEXT_PUBLIC_SEAL_KEY_SERVERS, process.env.NEXT_PUBLIC_SEAL_THRESHOLD);

export function aad(kind: BlobKind, sha256: string): Uint8Array {
  return new TextEncoder().encode(`suifleet/seal/v1/${kind}/${sha256.toLowerCase()}`);
}

export function isEnvelope(bytes: Uint8Array): boolean {
  return bytes.length >= MAGIC.length && MAGIC.every((byte, i) => bytes[i] === byte);
}

/** A fresh release identity: policy ID || key epoch (u64 LE) || 32 random bytes. */
export function newIdentity(policyId: string, keyEpoch: string | number | bigint): string {
  const epoch = new Uint8Array(8);
  new DataView(epoch.buffer).setBigUint64(0, BigInt(keyEpoch), true);
  const nonce = crypto.getRandomValues(new Uint8Array(32));
  return `0x${toHex(new Uint8Array([...fromHex(normalizeId(policyId)), ...epoch, ...nonce]))}`;
}

/** Encrypts one blob for a release; rejects rather than ever returning plaintext. */
export type Sealer = (plaintext: Uint8Array, kind: BlobKind, sha256: string) => Promise<Uint8Array>;

function equal(a: ArrayLike<number>, b: ArrayLike<number>): boolean {
  return a.length === b.length && Array.from(a).every((byte, i) => byte === b[i]);
}

/**
 * Encrypt under `identity` in the fleet package's Seal namespace (its
 * original, first-version ID). Key servers are read from chain and, with
 * `verifyKeyServers`, must prove possession of their keys first.
 */
export function sealer(client: SuiGrpcClient, settings: SealSettings, namespace: string, identity: string, verifyKeyServers = true): Sealer {
  const seal = new SealClient({ suiClient: client, serverConfigs: settings.keyServers, verifyKeyServers });
  return async (plaintext, kind, sha256) => {
    const expected = aad(kind, sha256);
    const { encryptedObject } = await seal.encrypt({
      threshold: settings.threshold, packageId: namespace, id: identity, data: plaintext, aad: expected, demType: DemType.AesGcm256,
    });
    // Check what is about to be published, independently of the SDK call.
    const parsed = EncryptedObject.parse(encryptedObject);
    const ciphertext = parsed.ciphertext.Aes256Gcm;
    const hex = (value: string) => value.replace(/^0x/, "").toLowerCase();
    if (normalizeId(parsed.packageId) !== normalizeId(namespace) || hex(parsed.id) !== hex(identity)) {
      throw new Error("Seal encryption used another package or identity; nothing was uploaded");
    }
    if (!ciphertext || !ciphertext.aad || !equal(ciphertext.aad, expected) || ciphertext.blob.length !== plaintext.length + 16 || parsed.threshold !== settings.threshold) {
      throw new Error("Seal encryption produced an unexpected ciphertext; nothing was uploaded");
    }
    const envelope = new Uint8Array(MAGIC.length + encryptedObject.length);
    envelope.set(MAGIC);
    envelope.set(encryptedObject, MAGIC.length);
    return envelope;
  };
}
