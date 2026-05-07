import { WALRUS_AGGREGATOR, WALRUS_PUBLISHER } from "./constants";

export interface UploadResult {
  blobId: string;
  sha256: string;
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

async function uploadBlob(body: ArrayBuffer | string, contentType: string): Promise<string> {
  const res = await fetch(`${WALRUS_PUBLISHER}/v1/blobs?epochs=5`, {
    method: "PUT",
    headers: { "Content-Type": contentType },
    body,
  });

  if (!res.ok) {
    throw new Error(`Walrus upload failed: ${res.status} ${await res.text()}`);
  }

  const json = await res.json();
  const blobId: string =
    json?.newlyCreated?.blobObject?.blobId ?? json?.alreadyCertified?.blobId;

  if (!blobId) {
    throw new Error(`Unexpected Walrus response: ${JSON.stringify(json)}`);
  }

  return blobId;
}

export async function uploadArtifact(file: File): Promise<UploadResult> {
  const buffer = await file.arrayBuffer();

  const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
  const sha256 = Array.from(new Uint8Array(hashBuffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  const blobId = await uploadBlob(buffer, "application/octet-stream");
  return { blobId, sha256 };
}

export async function uploadRecipe(recipe: ComponentRecipe): Promise<string> {
  return uploadBlob(JSON.stringify(recipe, null, 2), "application/json");
}

export function blobDownloadUrl(blobId: string): string {
  return `${WALRUS_AGGREGATOR}/v1/${blobId}`;
}
