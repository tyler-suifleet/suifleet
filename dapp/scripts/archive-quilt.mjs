// Ciphertext-only transport. Identifiers and patch IDs are not authentication:
// callers must obtain expected sizes/digests from a verified signed manifest.
import { createHash } from 'node:crypto';
import { open } from 'node:fs/promises';

export const MAX_ITEMS = 64; // deliberately below QuiltV1's 666 limit
export const MAX_BYTES = 4 * 1024 * 1024;
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');

export async function readBounded(path, limit = MAX_BYTES) {
  const file = await open(path, 'r');
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 1 || stat.size > limit) throw Error('Spool file size');
    const bytes = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await file.read(bytes, offset, bytes.length - offset, offset);
      if (!bytesRead) throw Error('Truncated spool file');
      offset += bytesRead;
    }
    const extra = Buffer.alloc(1);
    if ((await file.read(extra, 0, 1, offset)).bytesRead) throw Error('Growing spool file');
    return bytes;
  } finally { await file.close(); }
}

export function validateItems(items) {
  if (!Array.isArray(items) || items.length < 2 || items.length > MAX_ITEMS) throw Error('Quilt item count');
  const identifiers = new Set();
  let total = 0;
  for (const item of items) {
    if (!item || !/^[a-zA-Z0-9_-]{1,64}$/.test(item.identifier)
        || identifiers.has(item.identifier) || !(item.contents instanceof Uint8Array)
        || !item.contents.length || (total += item.contents.length) > MAX_BYTES) throw Error('Quilt item bounds');
    identifiers.add(item.identifier);
  }
}

export async function prepareQuilt(storage, items) {
  validateItems(items);
  // Stable input order is persisted by the caller before any purchase.
  const { quilt, index } = await storage.encodeQuilt({ blobs: items });
  if (quilt.length > MAX_BYTES) throw Error('Encoded Quilt exceeds archive quota');
  if (index.patches.length !== items.length) throw Error('Incomplete Quilt index');
  const encoded = await storage.encodeBlob(quilt);
  return { encoded, size: quilt.length, items: items.map(item => ({
    identifier: item.identifier, size: item.contents.length, sha256: digest(item.contents),
  })) };
}

export async function readVerifiedItem(storage, blobId, expected) {
  if (!expected || !/^[a-zA-Z0-9_-]{1,64}$/.test(expected.identifier)
      || !Number.isSafeInteger(expected.size) || expected.size < 1 || expected.size > MAX_BYTES
      || !/^[a-f0-9]{64}$/.test(expected.sha256)) throw Error('Invalid authenticated item descriptor');
  // Resolve the identifier within the specified parent, never accept a patch
  // ID supplied by an untrusted indexer as an independent content commitment.
  const blob = await storage.getBlob({ blobId });
  const files = await blob.files();
  if (files.length > MAX_ITEMS || files.length < 2) throw Error('Quilt index bounds');
  const matches = [];
  for (const file of files) if (await file.getIdentifier() === expected.identifier) matches.push(file);
  if (matches.length !== 1) throw Error('Missing or ambiguous Quilt item');
  const bytes = await matches[0].bytes();
  if (bytes.length !== expected.size || digest(bytes) !== expected.sha256) throw Error('Quilt item digest mismatch');
  return bytes;
}
