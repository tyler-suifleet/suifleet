import { WALRUS_AGGREGATOR, WALRUS_PUBLISHER } from "./constants";

export interface UploadResult {
  blobId: string;
  sha256: string;
}

export async function uploadFirmware(file: File): Promise<UploadResult> {
  const buffer = await file.arrayBuffer();

  // Compute sha256 client-side before upload
  const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
  const sha256 = Array.from(new Uint8Array(hashBuffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  const res = await fetch(`${WALRUS_PUBLISHER}/v1/blobs?epochs=5`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: buffer,
  });

  if (!res.ok) {
    throw new Error(`Walrus upload failed: ${res.status} ${await res.text()}`);
  }

  const json = await res.json();

  // Walrus returns either { newlyCreated: { blobObject: { blobId } } }
  // or { alreadyCertified: { blobId } }
  const blobId: string =
    json?.newlyCreated?.blobObject?.blobId ?? json?.alreadyCertified?.blobId;

  if (!blobId) {
    throw new Error(`Unexpected Walrus response: ${JSON.stringify(json)}`);
  }

  return { blobId, sha256 };
}

export function blobDownloadUrl(blobId: string): string {
  return `${WALRUS_AGGREGATOR}/v1/${blobId}`;
}
