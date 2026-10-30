import { NETWORK, WALRUS_AGGREGATOR, WALRUS_EPOCHS, WALRUS_MAX_COST_FROST, WALRUS_PUBLISHER, WALRUS_PUBLISHERS, WALRUS_AGGREGATORS } from "./constants";

/**
 * Who can renew the stored blob: only the owner of its Sui `Blob` object can
 * extend it. "fleet": held by the fleet's prepaid storage (SF-083), renewable
 * only with the fleet budget's owner capability.
 */
export type StorageOwner = "you" | "fleet" | "publisher" | "unknown";

export interface StorageInfo {
  publisherUrl?: string;
  blobId: string;
  /** Sui object ID of the Walrus `Blob`; needed to extend its storage period. */
  blobObjectId?: string;
  /** Last Walrus epoch the blob is stored for; devices cannot download it afterwards. */
  endEpoch?: number;
  /** Storage cost charged for this upload, in FROST. Absent when already certified. */
  costFrost?: bigint;
  owner: StorageOwner;
  /** True when identical bytes were already stored by someone else. */
  alreadyCertified: boolean;
}

export interface UploadResult {
  blobId: string;
  /** SHA-256 of the plaintext, which deployments record and devices verify. */
  sha256: string;
  storage: StorageInfo;
  /** True when the stored blob is a Seal-encrypted envelope. */
  confidential: boolean;
}

export interface UploadProgress {
  phase: "preparing" | "uploading" | "processing" | "complete";
  loaded: number;
  total?: number;
}
export type UploadObserver = (progress: UploadProgress) => void;

/**
 * Stores already protected bytes somewhere other than the publisher: the
 * fleet's prepaid storage (lib/fleet-upload.ts). Never a fallback: when
 * given, it is the only path.
 */
export type BlobStore = (body: Uint8Array, progress?: UploadObserver) => Promise<StorageInfo>;

export type { ComponentRecipe } from "./recipe-schema";
import { ComponentRecipe, assertComponentRecipe } from "./recipe-schema";
import { isEnvelope, type BlobKind, type Sealer } from "./seal";

/**
 * How a blob is stored (SF-022). Confidential is the customer path: the bytes
 * are Seal-encrypted in this browser before any upload. Public stores the
 * plaintext and is only for deliberately public or open-source content.
 * There is no default and no fallback from one to the other.
 */
export type Protection = { kind: "confidential"; seal: Sealer } | { kind: "public" };

type PublisherResponse = {
  newlyCreated?: {
    blobObject?: { id?: string; blobId?: string; storage?: { endEpoch?: number } };
    cost?: number | string;
  };
  alreadyCertified?: { blobId?: string; endEpoch?: number; object?: string };
};

/** Parse a Walrus publisher response into what the operator must track. */
export function parseStorage(json: PublisherResponse, owner: string | null): StorageInfo {
  const created = json.newlyCreated;
  if (created?.blobObject?.blobId) {
    return {
      blobId: created.blobObject.blobId,
      blobObjectId: created.blobObject.id,
      endEpoch: created.blobObject.storage?.endEpoch,
      costFrost: created.cost !== undefined ? BigInt(created.cost) : undefined,
      owner: owner ? "you" : "publisher",
      alreadyCertified: false,
    };
  }
  const certified = json.alreadyCertified;
  if (certified?.blobId) {
    return {
      blobId: certified.blobId,
      blobObjectId: certified.object,
      endEpoch: certified.endEpoch,
      owner: "unknown",
      alreadyCertified: true,
    };
  }
  throw new Error(`Unexpected Walrus response: ${JSON.stringify(json)}`);
}

/** Conditions the operator should act on before deploying. */
export function storageWarnings(storage: StorageInfo): string[] {
  const warnings: string[] = [];
  if (storage.endEpoch === undefined) {
    warnings.push("The publisher did not report a storage end epoch; expiry cannot be tracked.");
  }
  if (storage.alreadyCertified) {
    warnings.push(
      "These bytes were already stored by another upload. You do not own that Blob object, so you cannot renew it; " +
        "devices fail once its storage period ends.",
    );
  } else if (storage.owner !== "you" && storage.owner !== "fleet") {
    warnings.push("The Blob object was kept by the publisher, so you cannot renew it. Connect a wallet before uploading.");
  }
  if (WALRUS_MAX_COST_FROST !== null && storage.costFrost !== undefined && storage.costFrost > WALRUS_MAX_COST_FROST) {
    warnings.push(`Storage cost ${storage.costFrost} FROST exceeds the configured budget of ${WALRUS_MAX_COST_FROST} FROST.`);
  }
  return warnings;
}

/** Command an owner runs to keep a blob available beyond its end epoch. */
export function renewalCommand(storage: StorageInfo): string | null {
  // Fleet blobs are wrapped in fleet storage: extend them with fleet_storage::extend_blob.
  if (storage.owner === "fleet") return null;
  return storage.blobObjectId ? `walrus extend --blob-obj-id ${storage.blobObjectId} --epochs-extended ${WALRUS_EPOCHS}` : null;
}

/** The bytes to upload: an encrypted envelope, or the plaintext when explicitly public. */
export async function protect(plaintext: Uint8Array, kind: BlobKind, sha256: string, protection: Protection): Promise<Uint8Array> {
  if (protection.kind === "public") return plaintext;
  if (protection.kind !== "confidential") throw new Error("Choose confidential or explicitly public delivery");
  // Any failure here aborts the upload: never retry or fall back as plaintext.
  const sealed = await protection.seal(plaintext, kind, sha256);
  if (!isEnvelope(sealed) || sealed.length <= plaintext.length) throw new Error("Encryption did not produce a Seal envelope; nothing was uploaded");
  return sealed;
}

class TransientPublisherError extends Error {}
class PublisherHttpError extends Error {
  constructor(public status: number, body: string) { super(`Walrus upload failed: HTTP ${status} ${body}`); }
}
class UploadCancelledError extends Error {}
export class PublisherError extends Error {
  constructor(public endpoint: string, public details: string, reason = "publisher response or connection error") {
    super(`Upload to ${endpoint} failed (${reason}). Try again or add another publisher in Storage endpoints.`);
  }
}
let sessionPublishers: string[] = [];
const sessionKey = `suifleet:publishers:${NETWORK}:${WALRUS_PUBLISHER}`;
export function sessionEndpoints(): string[] {
  if (typeof sessionStorage === "undefined") return sessionPublishers;
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(sessionKey) ?? "[]");
    if (Array.isArray(saved) && saved.length <= 16) return saved.map(validatePublisher);
  } catch { /* Invalid saved overrides are ignored. */ }
  return sessionPublishers;
}
export function validatePublisher(value: string): string {
  const url = new URL(value.trim());
  if ((url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) || url.username || url.password || url.search || url.hash) throw new Error("Use an HTTPS publisher URL without credentials, query or fragment (HTTP is allowed on loopback).");
  return url.href.replace(/\/$/, "");
}
export function addSessionPublisher(value: string): string {
  const url = validatePublisher(value);
  const next = [...new Set([url, ...sessionEndpoints()])].slice(0, 16);
  if (typeof sessionStorage !== "undefined") sessionStorage.setItem(sessionKey, JSON.stringify(next));
  sessionPublishers = next;
  return url;
}

export function removeSessionPublisher(value: string): void {
  const next = sessionEndpoints().filter(url => url !== value);
  if (typeof sessionStorage !== "undefined") sessionStorage.setItem(sessionKey, JSON.stringify(next));
  sessionPublishers = next;
}


// Preserve the existing default Testnet failover for legacy single-URL manifests.
// Explicit ordered lists and session overrides select their own routes.
const TESTNET_PUBLISHERS = [
  "https://publisher.walrus-testnet.walrus.space",
  "https://publisher.walrus-testnet.h2o-nodes.com",
  "https://walrus-publisher-testnet.n1stake.com",
];

export async function uploadBlob(body: Uint8Array, contentType: string, owner: string | null, progress?: UploadObserver, epochs = WALRUS_EPOCHS): Promise<StorageInfo> {
  if (!Number.isSafeInteger(epochs) || epochs < 1) throw new Error("Choose a positive storage period");
  const defaults = NETWORK === "testnet" && WALRUS_PUBLISHER === TESTNET_PUBLISHERS[0]
    ? TESTNET_PUBLISHERS : [WALRUS_PUBLISHER];
  const overrides = sessionEndpoints();
  const publishers = [...new Set([...overrides, ...(WALRUS_PUBLISHERS ?? defaults)])];
  const failures: string[] = [];
  // Bound fallback attempts, allowing transfer time at 128 KiB/s plus two
  // minutes for certification. Explicit custom/mainnet routes retain no deadline.
  const timeoutMs = publishers.length > 1 ? 120_000 + Math.ceil(body.byteLength / 131_072) * 1_000 : undefined;
  for (const [index, publisher] of publishers.entries()) {
    try {
      const storage = await uploadToPublisher(publisher, body, contentType, owner, progress, timeoutMs, epochs);
      return { ...storage, publisherUrl: publisher };
    } catch (error) {
      // Content-addressed bytes are unchanged, and public publishers pay for
      // storage. Never replay a wallet transaction; only session overrides may
      // fall through after a rejected request or malformed response.
      if (error instanceof UploadCancelledError) throw error;
      failures.push(`${publisher}: ${error instanceof Error ? error.message : String(error)}`);
      if ((!(error instanceof TransientPublisherError) && !overrides.includes(publisher)) || index === publishers.length - 1) {
        const reason = error instanceof PublisherHttpError ? `HTTP ${error.status}`
          : error instanceof TransientPublisherError ? "publisher unavailable or network failure" : "invalid publisher response";
        throw new PublisherError(publisher, failures.join("\n"), reason);
      }
    }
  }
  throw new Error("No Walrus publisher configured");
}

async function uploadToPublisher(publisher: string, body: Uint8Array, contentType: string, owner: string | null, progress?: UploadObserver, timeoutMs?: number, epochs = WALRUS_EPOCHS): Promise<StorageInfo> {
  const params = new URLSearchParams({ epochs: String(epochs) });
  // Send the Blob object to the operator so they, not the publisher, can extend it.
  if (owner) params.set("send_object_to", owner);
  // Fetch does not expose request-body progress. Use browser upload events,
  // never a timer estimating storage/certification completion.
  if (progress && typeof XMLHttpRequest !== "undefined") {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.upload.addEventListener("progress", (event) => progress({
        phase: "uploading", loaded: event.loaded,
        total: event.lengthComputable ? event.total : undefined,
      }));
      request.upload.addEventListener("load", () => progress({ phase: "processing", loaded: body.byteLength }));
      request.onerror = () => reject(new TransientPublisherError("Walrus upload failed: network error"));
      request.onabort = () => reject(new UploadCancelledError("Walrus upload cancelled"));
      request.ontimeout = () => reject(new TransientPublisherError("Walrus upload timed out"));
      request.onload = () => {
        try {
          if (request.status < 200 || request.status >= 300) throw request.status >= 500 ? new TransientPublisherError(`Walrus upload failed: HTTP ${request.status} ${request.responseText}`) : new PublisherHttpError(request.status, request.responseText);
          const storage = parseStorage(JSON.parse(request.responseText), owner);
          progress({ phase: "complete", loaded: body.byteLength, total: body.byteLength });
          resolve(storage);
        } catch (error) { reject(error); }
      };
      request.open("PUT", `${publisher}/v1/blobs?${params}`);
      if (timeoutMs !== undefined) request.timeout = timeoutMs;
      request.setRequestHeader("Content-Type", contentType);
      progress({ phase: "uploading", loaded: 0, total: body.byteLength });
      request.send(new Blob([body as BlobPart]));
    });
  }
  progress?.({ phase: "processing", loaded: 0 });
  let res: Response;
  try {
    res = await fetch(`${publisher}/v1/blobs?${params}`, {
      method: "PUT",
      headers: { "Content-Type": contentType },
      body: new Blob([body as BlobPart]),
      ...(timeoutMs === undefined ? {} : { signal: AbortSignal.timeout(timeoutMs) }),
    });
  } catch {
    throw new TransientPublisherError("Walrus upload failed: network error or timeout");
  }

  if (!res.ok) {
    const body = await res.text();
    throw res.status >= 500 ? new TransientPublisherError(`Walrus upload failed: HTTP ${res.status} ${body}`) : new PublisherHttpError(res.status, body);
  }
  const storage = parseStorage(await res.json(), owner);
  progress?.({ phase: "complete", loaded: body.byteLength, total: body.byteLength });
  return storage;
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return Array.from(new Uint8Array(hash)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function uploadArtifact(file: File, owner: string | null, protection: Protection, progress?: UploadObserver, store?: BlobStore): Promise<UploadResult> {
  progress?.({ phase: "preparing", loaded: 0 });
  const plaintext = new Uint8Array(await file.arrayBuffer());
  const sha256 = await sha256Hex(plaintext);
  const body = await protect(plaintext, "artifact", sha256, protection);
  const storage = store ? await store(body, progress) : await uploadBlob(body, "application/octet-stream", owner, progress);
  return { blobId: storage.blobId, sha256, storage, confidential: protection.kind === "confidential" };
}

export async function uploadRecipe(recipe: ComponentRecipe, owner: string | null, protection: Protection, progress?: UploadObserver, store?: BlobStore): Promise<UploadResult> {
  progress?.({ phase: "preparing", loaded: 0 });
  assertComponentRecipe(recipe);
  const plaintext = new TextEncoder().encode(JSON.stringify(recipe, null, 2));
  const sha256 = await sha256Hex(plaintext);
  const body = await protect(plaintext, "recipe", sha256, protection);
  const storage = store ? await store(body, progress)
    : await uploadBlob(body, protection.kind === "public" ? "application/json" : "application/octet-stream", owner, progress);
  return { blobId: storage.blobId, sha256, storage, confidential: protection.kind === "confidential" };
}

export function blobDownloadUrl(blobId: string): string {
  return `${blobReadEndpoints.get(blobId) ?? WALRUS_AGGREGATORS?.[0] ?? WALRUS_AGGREGATOR}/v1/blobs/${encodeURIComponent(blobId)}`;
}

// Used by the archive readers too; explicit injected/custom transports keep
// their own behavior and every decrypted archive still passes its digest checks.
export const aggregatorFetch: typeof fetch = async (input, init) => {
  const url = String(input);
  for (const endpoint of new Set([WALRUS_AGGREGATOR, ...(WALRUS_AGGREGATORS ?? [])])) {
    if (!endpoint) continue;
    const prefix = `${endpoint.replace(/\/+$/, "")}/v1/blobs/`;
    if (url.startsWith(prefix) && !url.slice(prefix.length).includes("/")) return fetchBlob(decodeURIComponent(url.slice(prefix.length)), init);
  }
  return fetch(input, init);
};

export const blobReadEndpoints = new Map<string, string>();
export async function fetchBlob(blobId: string, init?: RequestInit): Promise<Response> {
  let last: Response | undefined;
  let uncertain = false;
  for (const endpoint of new Set([...(WALRUS_AGGREGATORS ?? []), WALRUS_AGGREGATOR].filter(Boolean))) {
    init?.signal?.throwIfAborted();
    try {
      // Respect the caller's whole-transfer budget, including body reads.
      const signal = init?.signal ?? AbortSignal.timeout(30_000);
      const response = await fetch(`${endpoint}/v1/blobs/${encodeURIComponent(blobId)}`, { ...init, signal });
      if (response.ok) {
        if (blobReadEndpoints.size >= 256) blobReadEndpoints.delete(blobReadEndpoints.keys().next().value!);
        blobReadEndpoints.set(blobId, endpoint); return response;
      }
      if (response.status !== 404 && response.status !== 410) uncertain = true;
      last = response;
      await response.body?.cancel();
    } catch { init?.signal?.throwIfAborted(); uncertain = true; }
  }
  if (uncertain) throw new Error("Walrus availability could not be determined; an aggregator failed");
  if (last) return last;
  throw new Error("No Walrus aggregator could read this blob");
}

export type Availability = "available" | "unavailable" | "unknown";

/** Probe one byte of a blob: 404/410 means expired or never stored. */
export async function probeBlob(blobId: string): Promise<Availability> {
  try {
    const res = await fetchBlob(blobId, { headers: { Range: "bytes=0-0" } });
    await res.body?.cancel();
    if (res.ok) return "available";
    return res.status === 404 || res.status === 410 ? "unavailable" : "unknown";
  } catch {
    return "unknown";
  }
}

export interface DeploymentStorage {
  recipe: Availability;
  artifact: Availability;
  /** The recipe is Seal-encrypted, so the artifact it names is not visible here. */
  confidential?: boolean;
  artifactBlobId?: string;
}

/** Whether devices can still fetch a deployment's recipe and the artifact it names. */
export async function probeDeploymentStorage(recipeBlobId: string): Promise<DeploymentStorage> {
  try {
    const res = await fetchBlob(recipeBlobId);
    if (res.status === 404 || res.status === 410) return { recipe: "unavailable", artifact: "unknown" };
    if (!res.ok) return { recipe: "unknown", artifact: "unknown" };
    const bytes = new Uint8Array(await res.arrayBuffer());
    if (isEnvelope(bytes)) return { recipe: "available", artifact: "unknown", confidential: true };
    const recipe: unknown = JSON.parse(new TextDecoder().decode(bytes));
    assertComponentRecipe(recipe);
    return { recipe: "available", artifact: await probeBlob(recipe.artifactBlobId), artifactBlobId: recipe.artifactBlobId, confidential: false };
  } catch {
    return { recipe: "unknown", artifact: "unknown" };
  }
}
