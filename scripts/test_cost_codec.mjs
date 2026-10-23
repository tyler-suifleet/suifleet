#!/usr/bin/env node
// Offline cross-check: pinned SDK encoding against two native publisher results.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const sdk = resolve(process.argv[2], 'node_modules/@mysten/walrus');
assert.equal(JSON.parse(readFileSync(resolve(sdk, 'package.json'))).version, '1.2.36');
const { encodedBlobLength, storageUnitsFromSize } = await import(pathToFileURL(resolve(sdk, 'dist/utils/index.mjs')));
const evidence = JSON.parse(readFileSync(new URL('../docs/business/sf054/walrus-testnet.json', import.meta.url)));
for (const { publisher_response } of evidence.uploads) {
  const blob = publisher_response.newlyCreated.blobObject;
  assert.equal(encodedBlobLength(blob.size, 1000), blob.storage.storageSize);
}
assert.equal(storageUnitsFromSize(1048576), 1);
assert.equal(storageUnitsFromSize(1048577), 2);
console.log('PASS: two native-publisher encoded sizes and MiB rounding boundaries');
