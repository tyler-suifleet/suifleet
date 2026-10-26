#!/usr/bin/env node
// Read-only gRPC v2 adapter. Accepts no wallet, keys, signing or execution options.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';

const [sdk, runPath, inventoryPath] = process.argv.slice(2);
const base = resolve(sdk, 'node_modules/@mysten/sui');
assert.equal(JSON.parse(readFileSync(resolve(base, 'package.json'))).version, '2.36.0');
const { SuiGrpcClient } = await import(pathToFileURL(resolve(base, 'dist/grpc/index.mjs')));
const { fromBase58, toHex } = await import(pathToFileURL(resolve(base, 'dist/utils/index.mjs')));
const chainId = value => /^[0-9a-f]{8}$/.test(value) ? value : toHex(fromBase58(value).slice(0, 4));
const run = JSON.parse(readFileSync(runPath));
assert.equal(run.schema_version, 1);
const { network, chain_identifier: expected, rpc_url: baseUrl } = run.deployment;
assert.ok(['mainnet', 'testnet', 'localnet'].includes(network));
if (network === 'mainnet') assert.equal(expected, '35834a8a');
if (network === 'testnet') assert.equal(expected, '4c78adac');
const inventory = inventoryPath ? JSON.parse(readFileSync(inventoryPath)) : { transactions: run.transactions };
assert.ok(Array.isArray(inventory.transactions));
assert.ok(inventory.transactions.length <= run.workload.max_transactions);
assert.equal(new Set(inventory.transactions.map(t => t.digest)).size, inventory.transactions.length);
const endpoint = new URL(baseUrl);
assert.ok(['http:', 'https:'].includes(endpoint.protocol) && !endpoint.username && !endpoint.password);
const client = new SuiGrpcClient({ network, baseUrl });
const chain = await client.core.getChainIdentifier({ signal: AbortSignal.timeout(30000) });
assert.equal(chainId(chain.chainIdentifier), expected, 'live chain mismatch');
const records = [];
for (const { digest, category } of inventory.transactions) {
  assert.match(digest, /^[1-9A-HJ-NP-Za-km-z]{32,44}$/);
  let response;
  try {
    response = await client.core.getTransaction({ digest,
      include: { transaction: true, effects: true, balanceChanges: true },
      signal: AbortSignal.timeout(30000) });
  } catch (error) {
    records.push({ type: 'transaction', id: digest, category, status: 'unknown',
      at: run.interval.start, source: baseUrl,
      reason: 'Read unavailable; not proof of non-execution', error: String(error) });
    continue;
  }
  const tx = response.Transaction ?? response.FailedTransaction;
  assert.equal(tx.digest, digest);
  assert.equal(typeof tx.status?.success, 'boolean');
  assert.equal(tx.effects?.transactionDigest, digest);
  assert.ok(tx.timestampMs && tx.effects?.gasUsed && tx.transaction?.gasData?.owner);
  records.push({ type: 'transaction', id: digest, category,
    status: tx.status.success ? 'success' : 'failure',
    at: new Date(tx.timestampMs).toISOString(), source: baseUrl,
    sender: tx.transaction.sender, gas_owner: tx.transaction.gasData.owner,
    gas: tx.effects.gasUsed, checkpoint: tx.checkpoint,
    balance_changes: tx.balanceChanges, raw: tx });
}
assert.equal(chainId((await client.core.getChainIdentifier({ signal: AbortSignal.timeout(30000) })).chainIdentifier), expected);
console.log(JSON.stringify({ schema_version: 1, network, chain_identifier: expected,
  run_id: run.run_id, observed_at: new Date().toISOString(),
  inventory, reader: '@mysten/sui 2.36.0 gRPC v2', read_only: true, transactions_submitted: 0,
  records }, (_, v) => typeof v === 'bigint' ? v.toString() : v, 2));
