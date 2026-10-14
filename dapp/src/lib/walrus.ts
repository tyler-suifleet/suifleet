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
  sha256: string;
  storage: StorageInfo;
}

export interface ComponentRecipe {
  schemaVersion: "1.0";
  componentName: string;
  componentVersion: string;
  description?: string;
  artifactBlobId: string;
  artifactSha256: string;
  lifecycle: {
    install?: string;
    run?: string;
    shutdown?: string;
  };
  configuration: Record<string, unknown>;
}

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

async function uploadBlob(body: ArrayBuffer | string, contentType: string, owner: string | null): Promise<StorageInfo> {
  const params = new URLSearchParams({ epochs: String(WALRUS_EPOCHS) });
  // Send the Blob object to the operator so they, not the publisher, can extend it.
  if (owner) params.set("send_object_to", owner);
  const res = await fetch(`${WALRUS_PUBLISHER}/v1/blobs?${params}`, {
    method: "PUT",
    headers: { "Content-Type": contentType },
    body,
  });

  if (!res.ok) {
    throw new Error(`Walrus upload failed: ${res.status} ${await res.text()}`);
  }
  return parseStorage(await res.json(), owner);
}

async function sha256Hex(bytes: BufferSource): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(hash)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function uploadArtifact(file: File, owner: string | null): Promise<UploadResult> {
  const buffer = await file.arrayBuffer();
  const sha256 = await sha256Hex(buffer);
  const storage = await uploadBlob(buffer, "application/octet-stream", owner);
  return { blobId: storage.blobId, sha256, storage };
}

export async function uploadRecipe(recipe: ComponentRecipe, owner: string | null): Promise<UploadResult> {
  const text = JSON.stringify(recipe, null, 2);
  const sha256 = await sha256Hex(new TextEncoder().encode(text));
  const storage = await uploadBlob(text, "application/json", owner);
  return { blobId: storage.blobId, sha256, storage };
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
}

/** Whether devices can still fetch a deployment's recipe and the artifact it names. */
export async function probeDeploymentStorage(recipeBlobId: string): Promise<DeploymentStorage> {
  try {
    const res = await fetch(blobDownloadUrl(recipeBlobId));
    if (res.status === 404 || res.status === 410) return { recipe: "unavailable", artifact: "unknown" };
    if (!res.ok) return { recipe: "unknown", artifact: "unknown" };
    const recipe = (await res.json()) as Partial<ComponentRecipe>;
    if (!recipe.artifactBlobId) return { recipe: "available", artifact: "unknown" };
    return { recipe: "available", artifact: await probeBlob(recipe.artifactBlobId) };
  } catch {
    return { recipe: "unknown", artifact: "unknown" };
  }
}
