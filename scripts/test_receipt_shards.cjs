// Real BCS/derived-field readers with independent authority and pagination refusals.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const fixture = structuredClone(require('../manifests/fixture.localnet.json'));
const { bcs, TypeTagSerializer } = appRequire('@mysten/sui/bcs');
const { deriveDynamicFieldID, normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
const catalog = require('../interfaces/fleet-v1.json');
for (const name of catalog.optional_type_origins.receipt_shards) fixture.type_origins[name] = id('0x99');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const load = loader(), p = load('lib/fleet-protocol.ts'), shards = load('lib/receipt-shards.ts'), app = load('lib/sui.ts');
const type = name => `${fixture.type_origins[name]}::${name}`;
const deployment = id('0xd'), roots = Array.from({length:16}, (_, i) => id('0x' + (100 + i).toString(16)));
const directory = { roots, original_schema:'1', migrated:'16', ready:true, retiring:false, remaining:'16' };
let wrongBinding = false, missing = false, badStatus = false;
const fieldObjects = new Map();
function putField(parent, keyName, key, valueType, value) {
  const keyType = type(`deployment_manager::${keyName}`), objectId = deriveDynamicFieldID(parent, TypeTagSerializer.parseFromStr(keyType), key);
  const bytes = new Uint8Array(32 + key.length + value.length); bytes.set(bcs.Address.serialize(objectId).toBytes()); bytes.set(key,32); bytes.set(value,32+key.length);
  fieldObjects.set(objectId, { objectId, type:`0x2::dynamic_field::Field<${keyType},${valueType}>`, content:bytes });
}
// A single root with 65 receipts tests the 64-entry page boundary and retry cursor.
const devices = Array.from({length:65}, (_, i) => id('0x' + ((i+1)*16).toString(16)));
for (let i=0; i<devices.length; i++) {
  putField(roots[0], 'ReceiptIndexKey', bcs.u64().serialize(i).toBytes(), '0x2::object::ID', bcs.Address.serialize(devices[i]).toBytes());
  putField(roots[0], 'ReceiptKey', bcs.Address.serialize(devices[i]).toBytes(), type('deployment_manager::ShardedReceipt'), p.deployment_manager_ShardedReceiptBcs.serialize({status:1, failed:false, reported_v3:true}).toBytes());
}
const client = {
  getDynamicField: async ({name}) => {
    if (name.type.endsWith('::ReceiptsKey')) return { dynamicField: { value: { type:type('deployment_manager::Receipts'), bcs:p.deployment_manager_ReceiptsBcs.serialize({version:4,statuses:{contents:[]}}).toBytes() } } };
    return { dynamicField: { value: { type:type('deployment_manager::ReceiptDirectory'), bcs:p.deployment_manager_ReceiptDirectoryBcs.serialize(directory).toBytes() } } };
  },
  getObject: async ({objectId}) => { const index = roots.indexOf(objectId); return { object: {
    objectId, owner:{Shared:{initialSharedVersion:'1'}}, type:type('deployment_manager::ReceiptShard'),
    content:p.deployment_manager_ReceiptShardBcs.serialize({id:objectId, deployment:wrongBinding?id('0xee'):deployment, index, count:index===0?65:0, migration_cursor:0, migrated:true}).toBytes()
  } }; },
  getObjects: async ({objectIds}) => ({objects:objectIds.map(objectId => { if (missing) return Error('missing'); const o = structuredClone(fieldObjects.get(objectId)); if (badStatus && o.type.includes('ShardedReceipt')) o.content[o.content.length-3] = 9; return o; })})
};
(async () => {
  assert.equal(shards.receiptShardIndex('0x10'), 0); assert.equal(shards.receiptShardIndex('0xff'), 15);
  const first = await shards.readReceiptPageV3(client,deployment,roots[0],0);
  assert.equal(first.next,64);assert.equal(first.hasMore,true);assert.equal(Object.keys(first.statuses).length,64);
  const last = await shards.readReceiptPageV3(client,deployment,roots[0],0,first.next);
  assert.equal(last.next,65);assert.equal(last.hasMore,false);assert.equal(Object.keys(last.statuses).length,1);
  const all = await app.readReceipts(client,deployment); assert.equal(all.verified,true);assert.equal(Object.keys(all.statuses).length,65);
  for (const state of [{ready:false},{retiring:true}]) {
    Object.assign(directory,state); assert.equal((await app.readReceipts(client,deployment)).verified,false);Object.assign(directory,{ready:true,retiring:false});
  }
  wrongBinding=true; await assert.rejects(app.readReceipts(client,deployment), /binding/);wrongBinding=false;
  missing=true; await assert.rejects(app.readReceipts(client,deployment), /missing/);missing=false;
  badStatus=true; await assert.rejects(app.readReceipts(client,deployment), /status/);badStatus=false;
  await assert.rejects(shards.readReceiptPageV3(client,deployment,roots[0],0,0,65), /page/);
  console.log('PASS: shard BCS/derived-field pagination, full v3 authority, migration/retirement, foreign root, missing field and malformed status refusals');
})().catch(error => { console.error(error); process.exitCode=1; });
