// Real BCS, hashes and device signatures; no provider or chain-success mocks.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
const { createHash } = require('node:crypto');
const fixture = require('../manifests/fixture.localnet.json');
fixture.chain_identifier = '4c78adac'; fixture.shared_objects.registry.id = id('0xa1');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const bulk = loader()('lib/fleet-bulk.ts');
const config = { schema: 1, chainIdentifier: '4c78adac', registryId: id('0xa1'), packageId: id('0xa2'), typeOrigin: id('0xa2'), fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0xb2') };
const key = Ed25519Keypair.generate(), rotated = Ed25519Keypair.generate(), device = id('0xd1');
function anchor(seq, signer=key) {return {seq:String(seq),kind:0,blob_object_id:id('0xc'+seq),blob_id:String(seq),ciphertext_sha256:Array(32).fill(seq),signer:signer.toSuiAddress(),at_ms:'1000000',end_epoch:100,key_epoch:'0'};}
const first = {first_index:'4',index:'4',anchor:anchor(5),previous_hash:[]};
const encoded = bulk.CompactHeadBcs.serialize(first).toBytes();
const second = {first_index:'4',index:'7',anchor:anchor(8,rotated),previous_hash:[...createHash('sha256').update(encoded).digest()]};
function archive(seq, previous, signer=key) {return {schema:3,kind:'fleet-data',chain_identifier:config.chainIdentifier,registry:config.registryId,fleet_id:config.fleetId,device_id:device,signer:signer.toSuiAddress(),seq,observed_ms:1000000,reason:'logs',dropped_lines:0,records:[{source:'daemon',line:'ready'}],previous_head:[...previous]};}
async function signed(a, signer) {const message=Buffer.from(JSON.stringify(a)); const s=await signer.signPersonalMessage(message);return Buffer.from(JSON.stringify({message:message.toString('base64'),signature:s.signature}));}
(async()=>{
 assert.deepEqual(bulk.parseCompactHead(encoded).anchor,first.anchor);
 assert.equal(bulk.parseCompactHead(encoded).index,first.index);
 const a=archive(8,encoded,rotated);
 const verified=await bulk.verifyArchive(await signed(a,rotated),config,device,second.anchor);
 assert.deepEqual(await bulk.previousCompactHead(verified,second),bulk.parseCompactHead(encoded));
 assert.equal(await bulk.previousCompactHead(archive(5,[]),first),undefined);
 // A rotated current writer cannot forge or truncate the old committed signer/history.
 await assert.rejects(bulk.previousCompactHead({...verified,previous_head:[]},second));
 const forged=bulk.CompactHeadBcs.serialize({...first,anchor:anchor(5,rotated)}).toBytes();
 await assert.rejects(bulk.previousCompactHead({...verified,previous_head:[...forged]},second));
 for(const edit of [{index:'7'},{first_index:'3'},{anchor:anchor(8)}]){
   const bytes=bulk.CompactHeadBcs.serialize({...first,...edit}).toBytes();
   await assert.rejects(bulk.previousCompactHead({...verified,previous_head:[...bytes]}, {...second,previous_hash:[...createHash('sha256').update(bytes).digest()]}));
 }
 for(const bad of [null,[-1],[256],[0.5],Array(513).fill(0)]){
   const v={...a,previous_head:bad};
   await assert.rejects(bulk.verifyArchive(await signed(v,rotated),config,device,second.anchor));
 }
 await assert.rejects(bulk.verifyArchive(await signed(a,key),config,device,second.anchor));
 await assert.rejects(bulk.previousCompactHead({...a,schema:2},second));
 assert.throws(()=>bulk.parseCompactHead(new Uint8Array(513)));
 assert.throws(()=>bulk.parseCompactHead(bulk.CompactHeadBcs.serialize({...first,first_index:'5'}).toBytes()));
 // Exact bytes shared with the native BCS parser; no private signing material.
 const fs=require('node:fs');
 const vector=JSON.parse(fs.readFileSync('interfaces/compat/compact-head.json','utf8'));
 assert.equal(Buffer.from(bulk.CompactHeadBcs.serialize(vector.head).toBytes()).toString('hex'),vector.bcs_hex);
 const descriptors=[{identifier:'logs-1',size:128,sha256:'ab'.repeat(32)},{identifier:'diagnostics-2',size:256,sha256:'cd'.repeat(32)}];
 const manifest={...a,schema:4,records:[],items:descriptors};
 const verifiedManifest=await bulk.verifyArchive(await signed(manifest,rotated),config,device,second.anchor);
 assert.deepEqual(await bulk.previousCompactHead(verifiedManifest,second),bulk.parseCompactHead(encoded));
 for(const items of [[],descriptors.slice(0,1),[descriptors[0],descriptors[0]],
   [{...descriptors[0],identifier:'manifest'},descriptors[1]],
   [{...descriptors[0],size:0},descriptors[1]],
   [{...descriptors[0],sha256:'ff'},descriptors[1]],
   [{...descriptors[0],identifier:'../escape'},descriptors[1]],
   [{...descriptors[0],extra:true},descriptors[1]]]) {
   await assert.rejects(bulk.verifyArchive(await signed({...manifest,items},rotated),config,device,second.anchor));
 }
 await assert.rejects(bulk.verifyArchive(await signed({...manifest,records:a.records},rotated),config,device,second.anchor));
 await assert.rejects(bulk.verifyArchive(await signed({...manifest,seq:9},rotated),config,device,second.anchor));
 await assert.rejects(bulk.verifyArchive(await signed(manifest,key),config,device,second.anchor));
 console.log('PASS: compact archive BCS, real device signatures/rotation, predecessor hash, truncation, replay, bounds, and shared native vector');
})().catch(e=>{console.error(e);process.exitCode=1;});
