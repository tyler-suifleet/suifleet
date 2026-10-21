import { WALRUS_AGGREGATOR, WALRUS_EPOCHS, WALRUS_MAX_COST_FROST, WALRUS_PUBLISHER } from "./constants";

/** Who can renew the stored blob: only the owner of its Sui `Blob` object can extend it. */
export type StorageOwner = "you" | "publisher" | "unknown";

export interface StorageInfo {
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
  } else if (storage.owner !== "you") {
    warnings.push("The Blob object was kept by the publisher, so you cannot renew it. Connect a wallet before uploading.");
  }
  if (WALRUS_MAX_COST_FROST !== null && storage.costFrost !== undefined && storage.costFrost > WALRUS_MAX_COST_FROST) {
    warnings.push(`Storage cost ${storage.costFrost} FROST exceeds the configured budget of ${WALRUS_MAX_COST_FROST} FROST.`);
  }
  return warnings;
}

/** Command an owner runs to keep a blob available beyond its end epoch. */
export function renewalCommand(storage: StorageInfo): string | null {
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

async function uploadBlob(body: Uint8Array, contentType: string, owner: string | null, progress?: UploadObserver): Promise<StorageInfo> {
  const params = new URLSearchParams({ epochs: String(WALRUS_EPOCHS) });
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
      request.onerror = () => reject(new Error("Walrus upload failed: network error"));
      request.onabort = () => reject(new Error("Walrus upload cancelled"));
      request.ontimeout = () => reject(new Error("Walrus upload timed out"));
      request.onload = () => {
        try {
          if (request.status < 200 || request.status >= 300) throw new Error(`Walrus upload failed: ${request.status} ${request.responseText}`);
          const storage = parseStorage(JSON.parse(request.responseText), owner);
          progress({ phase: "complete", loaded: body.byteLength, total: body.byteLength });
          resolve(storage);
        } catch (error) { reject(error); }
      };
      request.open("PUT", `${WALRUS_PUBLISHER}/v1/blobs?${params}`);
      request.setRequestHeader("Content-Type", contentType);
      progress({ phase: "uploading", loaded: 0, total: body.byteLength });
      request.send(new Blob([body as BlobPart]));
    });
  }
  progress?.({ phase: "processing", loaded: 0 });
  const res = await fetch(`${WALRUS_PUBLISHER}/v1/blobs?${params}`, {
    method: "PUT",
    headers: { "Content-Type": contentType },
    body: new Blob([body as BlobPart]),
  });

  if (!res.ok) {
    throw new Error(`Walrus upload failed: ${res.status} ${await res.text()}`);
  }
  const storage = parseStorage(await res.json(), owner);
  progress?.({ phase: "complete", loaded: body.byteLength, total: body.byteLength });
  return storage;
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return Array.from(new Uint8Array(hash)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function uploadArtifact(file: File, owner: string | null, protection: Protection, progress?: UploadObserver): Promise<UploadResult> {
  progress?.({ phase: "preparing", loaded: 0 });
  const plaintext = new Uint8Array(await file.arrayBuffer());
  const sha256 = await sha256Hex(plaintext);
  const body = await protect(plaintext, "artifact", sha256, protection);
  const storage = await uploadBlob(body, "application/octet-stream", owner, progress);
  return { blobId: storage.blobId, sha256, storage, confidential: protection.kind === "confidential" };
}

export async function uploadRecipe(recipe: ComponentRecipe, owner: string | null, protection: Protection, progress?: UploadObserver): Promise<UploadResult> {
  progress?.({ phase: "preparing", loaded: 0 });
  assertComponentRecipe(recipe);
  const plaintext = new TextEncoder().encode(JSON.stringify(recipe, null, 2));
  const sha256 = await sha256Hex(plaintext);
  const body = await protect(plaintext, "recipe", sha256, protection);
  const storage = await uploadBlob(body, protection.kind === "public" ? "application/json" : "application/octet-stream", owner, progress);
  return { blobId: storage.blobId, sha256, storage, confidential: protection.kind === "confidential" };
}

export function blobDownloadUrl(blobId: string): string {
  return `${WALRUS_AGGREGATOR}/v1/blobs/${blobId}`;
}

export type Availability = "available" | "unavailable" | "unknown";

/** Probe one byte of a blob: 404/410 means expired or never stored. */
export async function probeBlob(blobId: string): Promise<Availability> {
  try {
    const res = await fetch(blobDownloadUrl(blobId), { headers: { Range: "bytes=0-0" } });
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
}

/** Whether devices can still fetch a deployment's recipe and the artifact it names. */
export async function probeDeploymentStorage(recipeBlobId: string): Promise<DeploymentStorage> {
  try {
    const res = await fetch(blobDownloadUrl(recipeBlobId));
    if (res.status === 404 || res.status === 410) return { recipe: "unavailable", artifact: "unknown" };
    if (!res.ok) return { recipe: "unknown", artifact: "unknown" };
    const bytes = new Uint8Array(await res.arrayBuffer());
    if (isEnvelope(bytes)) return { recipe: "available", artifact: "unknown", confidential: true };
    const recipe = JSON.parse(new TextDecoder().decode(bytes)) as Partial<ComponentRecipe>;
    if (!recipe.artifactBlobId) return { recipe: "available", artifact: "unknown" };
    return { recipe: "available", artifact: await probeBlob(recipe.artifactBlobId) };
  } catch {
    return { recipe: "unknown", artifact: "unknown" };
  }
}
