// Ciphertext-only transport. Identifiers and patch IDs are not authentication:
// callers must obtain expected sizes/digests from a verified signed manifest.
import { createHash } from 'node:crypto';
import { open } from 'node:fs/promises';
import { readArchiveQuilt } from '../src/lib/archive-quilt.ts';

export const MAX_ITEMS = 17; // deliberately below QuiltV1's 666 limit
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
  // Refuse an unsupported committee/format before registration, not after
  // publishing bytes the bounded browser reader cannot safely consume.
  readArchiveQuilt(quilt);
  const encoded = await storage.encodeBlob(quilt);
  return { encoded, size: quilt.length, items: items.map(item => ({
    identifier: item.identifier, size: item.contents.length, sha256: digest(item.contents),
  })) };
}

export async function readVerifiedItem(parentBytes, expected) {
  if (!expected || !/^[a-zA-Z0-9_-]{1,64}$/.test(expected.identifier)
      || !Number.isSafeInteger(expected.size) || expected.size < 1 || expected.size > MAX_BYTES
      || !/^[a-f0-9]{64}$/.test(expected.sha256)) throw Error('Invalid authenticated item descriptor');
  // Parse only an already streaming-bounded parent. SDK index/header readers
  // must not allocate directly from untrusted lengths before this validation.
  const bytes = readArchiveQuilt(parentBytes).get(expected.identifier);
  if (!bytes) throw Error('Missing or ambiguous Quilt item');
  if (bytes.length !== expected.size || digest(bytes) !== expected.sha256) throw Error('Quilt item digest mismatch');
  return bytes;
}
