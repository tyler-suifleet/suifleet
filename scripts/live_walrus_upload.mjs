#!/usr/bin/env node
// SF-083 testnet: the device side of a fleet-paid Walrus upload, with the
// pinned SDK installed privately (never an app dependency):
//   npm install --prefix /tmp/sf083-walrus-sdk --ignore-scripts --save-exact @mysten/walrus@1.2.36 @mysten/sui@2.37.0
//   node scripts/live_walrus_upload.mjs /tmp/sf083-walrus-sdk RPC_URL BLOB_FILE EXPECTED_WALRUS_PACKAGE
// 1. Encodes the blob and prints its RS2 blob ID, root hash and size.
// 2. Reads one stdin line: the Blob object ID fleet_storage::register_blob
//    created. Writes the slivers to the storage nodes and prints the
//    certificate for fleet_storage::certify_blob.
// 3. Reads one more stdin line once certified, reads the blob back from the
//    storage nodes and prints whether it matches.
// Holds no key: the device signs its on-chain calls with its own wallet.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const [sdk, rpc, file, expectedPackage] = process.argv.slice(2);
const load = (pkg, path) => import(pathToFileURL(resolve(sdk, 'node_modules', pkg, path)));
const version = (pkg) => JSON.parse(readFileSync(resolve(sdk, 'node_modules', pkg, 'package.json'))).version;
if (version('@mysten/walrus') !== '1.2.36' || version('@mysten/sui') !== '2.37.0') throw new Error('expected the pinned @mysten/walrus 1.2.36 and @mysten/sui 2.37.0');
const { SuiGrpcClient } = await load('@mysten/sui', 'dist/grpc/index.mjs');
const { bcs } = await load('@mysten/sui', 'dist/bcs/index.mjs');
const { walrus, blobIdToInt, TESTNET_WALRUS_PACKAGE_CONFIG } = await load('@mysten/walrus', 'dist/index.mjs');
const { signersToBitmap } = await load('@mysten/walrus', 'dist/utils/index.mjs');

const client = new SuiGrpcClient({ network: 'testnet', baseUrl: rpc }).$extend(walrus());
const system = await client.walrus.systemObject();
const state = await client.walrus.systemState();
// The live Walrus package must be the one fleet_storage is pinned to.
const norm = (id) => '0x' + id.replace(/^0x/, '').padStart(64, '0');
if (norm(system.package_id) !== norm(expectedPackage)) throw new Error(`live Walrus package ${system.package_id} is not the pinned ${expectedPackage}`);
if (TESTNET_WALRUS_PACKAGE_CONFIG.systemObjectId !== norm(system.id.id ?? system.id)) throw new Error('unexpected Walrus system object');

const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const next = async () => (await lines.next()).value?.trim();
const hex = (bytes) => Buffer.from(bytes).toString('hex');

const blob = readFileSync(file);
const encoded = await client.walrus.encodeBlob(blob);
console.log(JSON.stringify({ blob_id: encoded.blobId, blob_id_u256: blobIdToInt(encoded.blobId).toString(),
  root_hash_u256: BigInt(bcs.u256().parse(encoded.rootHash)).toString(), size: blob.length,
  n_shards: state.committee.n_shards, epoch: state.committee.epoch, walrus_package: norm(system.package_id) }));

const blobObjectId = await next();
if (!/^0x[0-9a-f]{64}$/.test(blobObjectId ?? '')) throw new Error('expected the registered Blob object ID');
const confirmations = await client.walrus.writeEncodedBlobToNodes({ blobId: encoded.blobId, metadata: encoded.metadata,
  sliversByNode: encoded.sliversByNode, deletable: false, objectId: blobObjectId });
const certificate = await client.walrus.certificateFromConfirmations({ confirmations, blobId: encoded.blobId, deletable: false, blobObjectId });
console.log(JSON.stringify({ confirmations: confirmations.filter(Boolean).length, nodes: confirmations.length,
  signature: hex(certificate.signature), signers_bitmap: hex(signersToBitmap(certificate.signers, state.committee.members.length)),
  message: hex(certificate.serializedMessage) }));

if ((await next()) !== 'read') throw new Error('expected "read"');
const read = await client.walrus.readBlob({ blobId: encoded.blobId });
console.log(JSON.stringify({ read_matches: Buffer.from(read).equals(blob), read_bytes: read.length }));
process.exit(0);
