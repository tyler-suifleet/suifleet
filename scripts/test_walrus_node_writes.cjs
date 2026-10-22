const assert=require('node:assert/strict');
(async()=>{
 const {writeBounded}=await import('../dapp/scripts/walrus-node-writes.mjs');
 let active=0,peak=0;const seen=[];
 const storage={async writeEncodedBlobToNode(r){active++;peak=Math.max(active,peak);seen.push(r.nodeIndex);
  try{assert.equal(r.objectId,'owned-blob');assert.equal(r.deletable,false);assert(r.signal instanceof AbortSignal);
   await new Promise(resolve=>setTimeout(resolve,2));if(r.nodeIndex%7===0)throw Error('node unavailable');return {node:r.nodeIndex};
  }finally{active--;}}};
 const encoded={blobId:'blob',metadata:{},sliversByNode:Array.from({length:100},(_,i)=>i)};
 const confirmations=await writeBounded(storage,encoded,'owned-blob');assert.equal(peak,8);assert.equal(new Set(seen).size,100);
 for(let i=0;i<100;i++)assert.deepEqual(confirmations[i],i%7===0?null:{node:i});
 await assert.rejects(writeBounded(storage,encoded,'owned-blob',100));
 const failed=await writeBounded({writeEncodedBlobToNode:async()=>{throw Error('offline')}},encoded,'owned-blob');
 assert(failed.every(v=>v===null),'unavailable nodes must never fabricate a confirmation');
 console.log('Bounded node writes preserve committee order and omit failed confirmations; SDK quorum verification remains required');
})().catch(e=>{console.error(e);process.exitCode=1});
