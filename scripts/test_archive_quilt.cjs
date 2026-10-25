// Offline actual pinned SDK geometry/patch reader, not network acceptance.
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const fs = require('node:fs/promises');
async function main() {
  const { prepareQuilt, validateItems, readVerifiedItem, digest, readBounded } = await import('../dapp/scripts/archive-quilt.mjs');
  const { encodeQuilt, WalrusBlob } = appRequire('@mysten/walrus');
  const root = path.dirname(appRequire.resolve('@mysten/walrus'));
  const { QuiltReader } = await import(pathToFileURL(path.join(root, 'files/readers/quilt.mjs')));
  const { getSourceSymbols } = await import(pathToFileURL(path.join(root, 'utils/index.mjs')));
  const items = [
    { identifier: 'manifest', contents: Buffer.from('synthetic encrypted manifest') },
    { identifier: 'logs-1', contents: Buffer.from([0, 255, 1, 2]) },
    { identifier: 'diagnostic-2', contents: Buffer.from('synthetic encrypted diagnostic') },
  ];
  const {readArchiveQuilt} = loader()('lib/archive-quilt.ts');
  let quilt, index;
  const storage = {
    encodeQuilt: async ({blobs}) => { const result = encodeQuilt({blobs, numShards:1000, encodingType:'RS2'}); quilt = result.quilt; index = result.index; return result; },
    // Offline encoding boundary: no certificate, gas or write success invented.
    encodeBlob: async bytes => ({ bytes }),
    getBlob: async () => {
      const { primarySymbols: rows, secondarySymbols: columns } = getSourceSymbols(1000, 'RS2');
      const symbolSize = quilt.length / (rows * columns);
      const reader = new QuiltReader({ blob: {blobId:Buffer.alloc(32).toString('base64url'), hasStartedLoadingFullBlob:true,
        getBytes:async()=>quilt, getRowSize:async()=>columns*symbolSize, getSymbolSize:async()=>symbolSize, getColumnSize:async()=>rows*symbolSize} });
      return new WalrusBlob({ reader: {getQuiltReader:async()=>reader} });
    },
  };
  const result = await prepareQuilt(storage, items);
  assert.equal(result.items.length, 3);
  const decoded = readArchiveQuilt(quilt);
  // Independently roundtrip the same valid fixture through the pinned SDK,
  // in addition to the bounded application parser.
  for(const file of await (await storage.getBlob()).files()) {
    const identifier=await file.getIdentifier();
    assert.deepEqual(Buffer.from(await file.bytes()),items.find(i=>i.identifier===identifier).contents);
  }

  for (const item of items) assert.deepEqual(Buffer.from(decoded.get(item.identifier)),item.contents);
  const writeLogical = (bytes, start, offset, value) => {
    const symbol = bytes.length/(334*667), column=334*symbol;
    for (let n=0;n<value.length;n++) {
      const position=offset+n,col=start+Math.floor(position/column),within=position%column;
      bytes[Math.floor(within/symbol)*667*symbol+col*symbol+within%symbol]=value[n];
    }
  };
  for(const [start,offset,value] of [[0,0,[99]],[0,1,[255,255,255,255]],[0,5,[255,255,255,127]],
    [index.patches[0].startIndex,1,[255,255,255,255]],[index.patches[0].startIndex,6,[255,255]]]) {
    const bad=Uint8Array.from(quilt);writeLogical(bad,start,offset,value);
    assert.throws(()=>readArchiveQuilt(bad),/Quilt/);
  }
  assert.throws(()=>readArchiveQuilt(quilt.subarray(1)),/geometry/);

  for (const expected of result.items) {
    assert.deepEqual(Buffer.from(await readVerifiedItem(quilt, expected)), items.find(i=>i.identifier===expected.identifier).contents);
  }
  await assert.rejects(readVerifiedItem(quilt, {...result.items[1], sha256:'00'.repeat(32)}), /digest/);
  await assert.rejects(readVerifiedItem(quilt, {...result.items[1], identifier:'missing'}), /Missing/);
  await assert.rejects(readVerifiedItem(quilt, {...result.items[1], size:5}), /digest/);
  assert.throws(()=>validateItems([items[0]]), /count/);
  assert.throws(()=>validateItems([items[0],items[0]]), /bounds/);
  assert.throws(()=>validateItems([{...items[0],identifier:'../escape'},items[1]]), /bounds/);
  assert.throws(()=>validateItems(Array.from({length:65},(_,i)=>({...items[0],identifier:String(i)}))), /count/);
  await assert.rejects(prepareQuilt(storage, [{identifier:'one',contents:Buffer.alloc(3*1024*1024)}, {identifier:'two',contents:Buffer.alloc(3*1024*1024)}]), /bounds/);
  for(const [count,size] of [[2,24576],[3,65536],[9,2048],[17,65536],[3,524288]]) {
    const blobs=Array.from({length:count},(_,i)=>({identifier:i===0?'manifest':`item-${i}`,contents:Buffer.alloc(size,i)}));
    const {quilt:encoded}=encodeQuilt({blobs,numShards:1000,encodingType:'RS2'});
    const parsed=readArchiveQuilt(encoded);
    assert.equal(parsed.size,count);
    for(const item of blobs) assert.deepEqual(Buffer.from(parsed.get(item.identifier)),item.contents);
  }
  const dir = await fs.mkdtemp('/tmp/sf121-quilt-');
  try {
    const file = path.join(dir,'item'); await fs.writeFile(file,items[1].contents);
    assert.equal(digest(await readBounded(file,4)),result.items[1].sha256);
    await assert.rejects(readBounded(file,3), /size/);
    await assert.rejects(readBounded(dir), /size/);
  } finally { await fs.rm(dir,{recursive:true}); }
  console.log('PASS actual pinned Quilt encode/read, mixed items, wrong/missing/digest/size/item-count/path/quota controls; offline only');
}
main().catch(error=>{console.error(error);process.exitCode=1});
