// Offline geometry experiment against the application's actual locked SDK.
// Random bytes model ciphertext lengths only: no encryption, storage-node
// roundtrip, authentication, gas, WAL price or production margin is claimed.
const { createHash, randomBytes } = require('node:crypto');
const fs = require('node:fs');
const { appRequire } = require('./frontend_test_utils.cjs');
const { encodeQuilt } = appRequire('@mysten/walrus');
const assert = require('node:assert/strict');
const walrusPackage = JSON.parse(fs.readFileSync(require('node:path').join(
  require('node:path').dirname(appRequire.resolve('@mysten/walrus')), '../package.json')));
assert.equal(walrusPackage.version, '1.2.35', 'Measure the reviewed pinned SDK');
async function main() {
const root = require('node:path').dirname(appRequire.resolve('@mysten/walrus'));
const {pathToFileURL} = require('node:url');
// Internal readers are used solely by this version-pinned offline experiment.
const {QuiltReader} = await import(pathToFileURL(require('node:path').join(root,'files/readers/quilt.mjs')));
const {getSourceSymbols} = await import(pathToFileURL(require('node:path').join(root,'utils/index.mjs')));
const numShards = 1000;
const cases = [];
for (const [items, itemBytes] of [[2,256],[8,256],[64,256],[2,2048],[8,2048],[64,2048],[2,65536],[8,65536],[666,256]]) {
  const blobs = Array.from({length:items}, (_, n) => ({
    identifier: `archive-${String(n).padStart(4,'0')}`, contents: randomBytes(itemBytes),
  }));
  const {quilt,index} = encodeQuilt({blobs,numShards,encodingType:'RS2'});
  assert.equal(index.patches.length,items);
  assert.equal(new Set(index.patches.map(p=>p.identifier)).size,items);
  assert.ok(index.patches.every(p=>p.startIndex < p.endIndex));
  const {primarySymbols:rows,secondarySymbols:columns} = getSourceSymbols(numShards,'RS2');
  const symbolSize = quilt.length/(rows*columns);
  assert.ok(Number.isInteger(symbolSize));
  // Explicit in-memory byte source; this does not simulate a successful
  // node write, certificate or authenticated network patch read.
  const reader = new QuiltReader({blob:{blobId:Buffer.alloc(32).toString('base64url'),
    hasStartedLoadingFullBlob:true,getBytes:async()=>quilt,
    getRowSize:async()=>columns*symbolSize,getSymbolSize:async()=>symbolSize,
    getColumnSize:async()=>rows*symbolSize}});
  const decoded = await reader.readIndex();
  assert.equal(decoded.length,items);
  for (const patch of decoded) {
    const original = blobs.find(b=>b.identifier===patch.identifier);
    assert.ok(original);
    assert.deepEqual(Buffer.from(await reader.readerForPatchId(patch.patchId).getBytes()),original.contents);
  }
  cases.push({items,item_bytes:itemBytes,total_ciphertext_bytes:items*itemBytes,
    packed_blob_bytes:items*itemBytes,quilt_blob_bytes:quilt.length,
    quilt_overhead_bytes:quilt.length-items*itemBytes,
    fits_current_4mib_archive_limit:quilt.length <= 4*1024*1024,
    // The current producer already packs multiple records into one archive.
    // Both alternatives need one registration, certification and data anchor.
    current_packed_transactions:3,quilt_transactions:3,health_transactions_removed:0});
}
assert.throws(()=>encodeQuilt({blobs:Array.from({length:667},(_,n)=>({identifier:String(n),contents:new Uint8Array([1])})),numShards,encodingType:'RS2'}), /Too many blobs/);
const report = {schema:1,scope:'offline synthetic ciphertext-length geometry only',
  sdk_version:walrusPackage.version,num_shards:numShards,encoding:'RS2',
  local_patch_roundtrip:true,network_roundtrip:false,
  source_sha256:createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),cases,
  limits:['No actual encryption/compression/erasure-coded sliver measurement or network writes.',
    'Packed bytes omit application framing; quilting includes SDK index and padding.',
    'One already-packed archive and one quilt both require three current runtime transactions.',
    'Cross-device aggregation, gas savings, WAL quotes and a subscription margin are not established.']};
if (process.argv.includes('--check')) {
  assert.deepEqual(report, JSON.parse(fs.readFileSync(require('node:path').join(__dirname, '../docs/validation/sf121-quilt-geometry.json'))));
  console.log('PASS: pinned Quilt geometry, per-item local roundtrip, item/size limits and committed experiment');
} else console.log(JSON.stringify(report,null,2));

}
main().catch(e=>{console.error(e);process.exitCode=1});
