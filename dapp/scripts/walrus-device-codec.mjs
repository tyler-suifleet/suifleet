#!/usr/bin/env node
// Local, keyless codec/storage-node worker. Run from the locked dApp SDK
// installation. It never signs transactions or receives archive plaintext.
import { readFile } from 'node:fs/promises';
import { writeBounded, requireWeightedQuorum } from './walrus-node-writes.mjs';
import { createInterface } from 'node:readline';
import { SuiGrpcClient } from '@mysten/sui/grpc';
import { walrus, TESTNET_WALRUS_PACKAGE_CONFIG, blobIdToInt } from '@mysten/walrus';
import { bcs } from '@mysten/sui/bcs';
import { normalizeSuiAddress as id, fromBase58, toHex } from '@mysten/sui/utils';
async function main() {
const input = createInterface({ input: process.stdin, crlfDelay: Infinity })[Symbol.asyncIterator]();
try {
  const first = await input.next();
  if (first.done || first.value.length > 16384) throw Error('Invalid codec request');
  const request = JSON.parse(first.value);
  if (request.chain !== '4c78adac') throw Error('Only the pinned testnet Walrus deployment is supported');
  const bytes = new Uint8Array(await readFile(request.ciphertext_path));
  if (!bytes.length || bytes.length > 4 * 1024 * 1024) throw Error('Archive size out of bounds');
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: request.rpc_url });
  const info = await client.getChainIdentifier();
  const genesis = fromBase58(info.chainIdentifier);
  if (genesis.length !== 32 || toHex(genesis.slice(0,4)) !== request.chain) throw Error('Foreign Sui chain');
  const storage = client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus;
  if (id(TESTNET_WALRUS_PACKAGE_CONFIG.systemObjectId) !== id(request.system_id)
      || id((await storage.systemObject()).package_id) !== id(request.walrus_package)) throw Error('Unsupported Walrus deployment');
  const committee = (await storage.systemState()).committee;
  const encoded = await storage.encodeBlob(bytes);
  const prepared = { blob_id: blobIdToInt(encoded.blobId).toString(), root_hash: bcs.u256().parse(encoded.rootHash).toString() };
  if (request.operation === 'prepare') console.log(JSON.stringify(prepared));
  else if (request.operation === 'registered') {
    const result = await client.waitForTransaction({ digest: request.digest, include: { effects: true, events: true } });
    if (result.FailedTransaction || !result.Transaction.status.success) throw Error('Registration did not succeed');
    const record = bcs.struct('UploadRegistered', { storage_id: bcs.Address, device_id: bcs.Address,
      blob_object_id: bcs.Address, blob_id: bcs.u256(), size: bcs.u64(), encoded_size: bcs.u64(), write_fee: bcs.u64(), end_epoch: bcs.u32(),
      encoded_bytes_in_period: bcs.u64(), uploads_in_period: bcs.u64(), pool_size: bcs.u64(), wal_balance: bcs.u64(), at_limit: bcs.bool() });
    let found;
    for (const event of result.Transaction.events ?? []) {
      if (event.eventType !== `${id(request.storage_type_origin)}::fleet_storage::UploadRegistered`) continue;
      const v = record.parse(event.bcs);
      if (id(v.storage_id) === id(request.storage_id) && id(v.device_id) === id(request.device_id) && v.blob_id === prepared.blob_id) found = v;
    }
    if (!found) throw Error('Registration event not found');
    console.log(JSON.stringify({ blob_object_id: found.blob_object_id, end_epoch: found.end_epoch, encoded_size: found.encoded_size, write_fee: found.write_fee }));
  } else if (request.operation === 'write') {
    if (prepared.blob_id !== request.blob_id || prepared.root_hash !== request.root_hash) throw Error('Changed archive encoding');
    const confirmations = await writeBounded(storage, encoded, request.blob_object_id);
    const cert = await storage.certificateFromConfirmations({ confirmations, blobId: encoded.blobId, deletable: false, blobObjectId: request.blob_object_id });
    const certificateCommittee = (await storage.systemState()).committee;
    requireWeightedQuorum(certificateCommittee, cert.signers);
    const bitmap = new Uint8Array(Math.ceil(certificateCommittee.members.length / 8));
    for (const n of cert.signers) { if (!Number.isInteger(n) || n < 0 || n >= certificateCommittee.members.length) throw Error('Invalid signer'); bitmap[n >> 3] |= 1 << (n % 8); }
    console.log(JSON.stringify({ signature: Buffer.from(cert.signature).toString('base64'), bitmap: Buffer.from(bitmap).toString('base64'), message: Buffer.from(cert.serializedMessage).toString('base64') }));
  } else throw Error('Unknown codec operation');
} catch {
  // Transport/SDK errors can contain URLs or request bodies. Keep logs categorical.
  console.error('Walrus codec operation failed'); process.exitCode = 1;
}

}
main().catch(()=>{ console.error("Walrus codec initialization failed"); process.exitCode=1; });
