#!/usr/bin/env node
// Independent, read-only gRPC + SDK check against the GraphQL rate snapshot.
// Audit environment only: @mysten/walrus 1.2.36 and @mysten/sui 2.37.0.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const [sdk, factsFile] = process.argv.slice(2);
const base = resolve(sdk, 'node_modules/@mysten');
for (const [name, version] of [['walrus', '1.2.36'], ['sui', '2.37.0']]) {
  assert.equal(JSON.parse(readFileSync(resolve(base, name, 'package.json'))).version, version);
}
const { WalrusClient } = await import(pathToFileURL(resolve(base, 'walrus/dist/index.mjs')));
const { SuiGrpcClient } = await import(pathToFileURL(resolve(base, 'sui/dist/grpc/index.mjs')));
const walrus = new WalrusClient({ network: 'mainnet', suiClient: new SuiGrpcClient({ network: 'mainnet', baseUrl: 'https://fullnode.mainnet.sui.io:443' }) });
const state = await walrus.systemState();
const facts = JSON.parse(readFileSync(factsFile));
for (const key of ['storage_price_per_unit_size', 'write_price_per_unit_size']) {
  assert.equal(String(state[key]), String(facts.walrus[key]), `${key}: snapshot changed or readers disagree`);
}
assert.equal(state.committee.epoch, facts.walrus.epoch);
const quotes = [];
for (const size of [1049122, 860]) quotes.push({ ciphertext_bytes: size, epochs: 2, ...await walrus.storageCost(size, 2) });
console.log(JSON.stringify({ network: 'mainnet', read_only: true, transactions_submitted: 0,
  reader: '@mysten/walrus 1.2.36 / @mysten/sui 2.37.0 gRPC; audit-only dependencies',
  epoch: state.committee.epoch, storage_price_per_unit_size: state.storage_price_per_unit_size,
  write_price_per_unit_size: state.write_price_per_unit_size, quotes,
  limits: 'SDK gross storage/write cost; no subsidy, transaction gas, provider invoice or purchase expiry.'
}, (_, value) => typeof value === 'bigint' ? value.toString() : value, 2));
