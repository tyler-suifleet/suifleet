#!/usr/bin/env node
// Read-only exact encoding/rate arithmetic, deliberately separate from the app
// dependencies. Install the pinned audit tool in a private temp directory:
// npm install --prefix /tmp/sf054-sdk --legacy-peer-deps --ignore-scripts --save-exact @mysten/walrus@1.2.36
// node scripts/quote_blob_cost.mjs /tmp/sf054-sdk evidence.json CIPHERTEXT EPOCHS
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [sdk, factsFile, blobFile, epochsText] = process.argv.slice(2);
const base = resolve(sdk, 'node_modules/@mysten/walrus');
const version = JSON.parse(readFileSync(resolve(base, 'package.json'))).version;
if (version !== '1.2.36') throw new Error('audit codec must be @mysten/walrus 1.2.36');
const utils = resolve(base, 'dist/utils/index.mjs');
const { encodedBlobLength, storageUnitsFromSize } = await import(pathToFileURL(utils));
const facts = JSON.parse(readFileSync(factsFile));
const bytes = readFileSync(blobFile);
const magic = Buffer.from('suifleet/seal/v1\n');
if (!bytes.subarray(0, magic.length).equals(magic)) throw new Error('expected SuiFleet Seal envelope');
const epochs = Number(epochsText);
if (!Number.isSafeInteger(epochs) || epochs < 1) throw new Error('positive integer epochs required');
const encodedBytes = encodedBlobLength(bytes.length, facts.walrus.n_shards);
const units = BigInt(storageUnitsFromSize(encodedBytes));
const storage = units * BigInt(facts.walrus.storage_price_per_unit_size) * BigInt(epochs);
const write = units * BigInt(facts.walrus.write_price_per_unit_size);
const tip = facts.relay.send_tip.kind.linear;
if (!tip) throw new Error('unsupported relay quote');
console.log(JSON.stringify({
  kind: 'read_only_rate_calculation', network: 'mainnet',
  source: factsFile, client_version: `@mysten/walrus ${version}`, encoding: 'RS2',
  codec_sha256: createHash('sha256').update(readFileSync(utils)).digest('hex'),
  ciphertext_sha256: createHash('sha256').update(bytes).digest('hex'), ciphertext_bytes: bytes.length,
  encoded_bytes: encodedBytes, units_mib: units.toString(), requested_epochs: epochs,
  observed_epoch: facts.walrus.epoch,
  storage_frost: storage.toString(), write_frost: write.toString(),
  gross_frost: (storage + write).toString(),
  relay_tip_mist: (BigInt(tip.base) + BigInt(tip.encoded_size_mul_per_kib) * ((BigInt(encodedBytes) + 1023n) / 1024n)).toString(),
  subsidy_frost: null, start_epoch: null, end_epoch: null,
  limits: 'Not a purchase quote: subsidy, publisher fees, expiry, Sui gas and deposits require actual client/publisher evidence. No storage bought.'
}, null, 2));
