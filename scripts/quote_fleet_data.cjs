#!/usr/bin/env node
// Read-only, reproducible cost/encoding quote. No wallet, signatures or writes.
const {createRequire}=require('node:module'),path=require('node:path');
const {pathToFileURL}=require('node:url');const r=createRequire(path.resolve('dapp/package.json'));
const {SuiGrpcClient}=r('@mysten/sui/grpc'),{walrus,TESTNET_WALRUS_PACKAGE_CONFIG,encodeQuilt}=r('@mysten/walrus');
const {fromBase58,toHex}=r('@mysten/sui/utils');
(async()=>{
 const client=new SuiGrpcClient({network:'testnet',baseUrl:process.env.SUIFLEET_QUOTE_RPC ?? 'https://fullnode.testnet.sui.io:443'});
 const info=await client.getChainIdentifier(),genesis=fromBase58(info.chainIdentifier),chain=toHex(genesis.slice(0,4));
 if(genesis.length!==32 || chain!=='4c78adac')throw Error('Foreign quote chain');
 const w=client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
 const system=await w.systemObject(),state=await w.systemState(),n=state.committee.n_shards;
 const {encodedBlobLength}=await import(pathToFileURL(path.join(path.dirname(r.resolve('@mysten/walrus')),'utils/index.mjs')));
 const samples=[1,2048,65536,1048576,2097152],quotes=[];
 for(const size of samples)for(const epochs of [3,30])quotes.push({plaintext_or_ciphertext_bytes:size,epochs,encoded_bytes:encodedBlobLength(size,n),...await w.storageCost(size,epochs)});
 const parts=Array.from({length:24},(_,i)=>({identifier:`log-${i}`,contents:new Uint8Array(2048)}));
 const quilt=encodeQuilt({blobs:parts,numShards:n,encodingType:'RS2'}).quilt;
 const single=await w.storageCost(2048,3),packed=await w.storageCost(24*2048,3),quiltCost=await w.storageCost(quilt.length,3);
 const encoded=await w.encodeBlob(new Uint8Array(2048));
 console.log(JSON.stringify({schema:1,observed_at:new Date().toISOString(),chain,sdk:{sui:'2.36.0',walrus:'1.2.35'},
  system_id:TESTNET_WALRUS_PACKAGE_CONFIG.systemObjectId,walrus_package:system.package_id,walrus_epoch:state.committee.epoch,n_shards:n,
  storage_price_frost_per_encoded_MiB_epoch:state.storage_price_per_unit_size,write_price_frost_per_encoded_MiB:state.write_price_per_unit_size,
  encoding_metadata_bytes:n*(n*64+32),actual_sdk_encoding:{input_bytes:2048,blob_id:encoded.blobId,root_hash_hex:Buffer.from(encoded.rootHash).toString('hex')},quotes,
  batching:{records:24,record_bytes:2048,separate_upload_frost:single.totalCost*24n,packed_bytes:24*2048,packed_upload_frost:packed.totalCost,
   quilt_bytes:quilt.length,quilt_upload_frost:quiltCost.totalCost,chosen:'one signed, Seal-encrypted batch per period; quilts add no fixed-overhead saving over one packed archive'},
  daily_monthly_testnet_frost:{uploads:30,epochs_per_upload:3,storage_and_write:single.totalCost*30n},
  limits:['Read-only live quotes and real SDK encoding; no paid storage/publication transaction.', 'Testnet prices and one-day epochs; not a Mainnet quote or token exchange quote.', 'Sui object/transaction gas, emergency uploads, encryption envelope size and retention must be added. Per-upload quote input is ciphertext bytes.']},(_,v)=>typeof v==='bigint'?v.toString():v,2));
})().catch(e=>{console.error(String(e));process.exit(1)});
