// Browser parity for incident thresholds and SF-098 recovery from verified
// current reports plus authoritative applied receipts; an HTTP relay is absent.
const assert=require('node:assert/strict');
const {loader}=require('./frontend_test_utils.cjs');
const {deriveChainAlerts}=loader()('lib/chain-alerts.ts');
const device={objectId:'0xd1',name:'pi'},now=5_000_000;
const healthy={status:'healthy',codes:[],reasons:[],components:[],observedMs:now,lastSeenMs:now,intervalSeconds:60};
const failed={deploymentId:'0xe1',componentName:'app',version:'1',timestampMs:1,receiptsVerified:true,resolvedDeviceIds:['0xd1'],statuses:{'0xd1':2}};
const applied={...failed,deploymentId:'0xe2',version:'0.9',timestampMs:2,statuses:{'0xd1':1}};
const installed={name:'app',version:'0.9',deployment_id:'0xe2',installed:true,supervised:true,phase:'running',restarts:0,last_exit:null,since_ms:3};
const result=(h=healthy,history=[])=>deriveChainAlerts([device],history,{'0xd1':h},now);
assert.equal(result().status,'ok');assert.equal(result().items.length,0);
assert.equal(result({...healthy,status:'unavailable'}).status,'unavailable');
assert.equal(result(healthy,[{...failed,receiptsVerified:false}]).status,'unavailable');
assert.deepEqual(result(healthy,[failed]).items.map(a=>a.kind),['deployment-failed']);
assert.equal(result({...healthy,components:[installed]},[failed,applied]).items.length,0,'confirmed installed older version is a valid rollback recovery');
for(const h of [{...healthy,components:[{...installed,installed:false}]},{...healthy,codes:['delayed'],components:[installed]},
  {...healthy,status:'offline',components:[installed]}, {...healthy,components:[installed,{...installed,phase:'broken'}]}]) {
  assert(result(h,[failed,applied]).items.some(a=>a.kind==='deployment-failed'),'no unconfirmed/delayed/offline/conflicting recovery');
}
assert(result(healthy,[{...failed,statuses:{}}]).items.some(a=>a.kind==='deployment-pending'));
assert(!result({...healthy,status:'offline'},[{...failed,statuses:{}}]).items.some(a=>a.kind==='deployment-pending'));
const queue={principal:'sink',subscriptions:1,queued:100,fill_percent:100,full_since_ms:now-120_000,refused:1,refused_recent:1,dropped:0,dropped_recent:0};
const service={version:1,messaging:{subscriptions:1,queued:100,refused:1,dropped:0,queues:[queue],omitted:0},
 bridge:{connected:false,since_ms:now-120_000,connects:0,refused:0,refused_recent:1,rejected:0,rejected_recent:0,last_error:'network'},
 streams:{streams:[{name:'events',bytes:90,max_size:100,fill_percent:90,messages:1,overwrite:true,rejected:1,rejected_recent:1,
 export:{lag:1,lost:1,lost_recent:1,batches:0,failures:3,consecutive_failures:3,last_success_ms:null,last_error:'network'}}],omitted:0},
 shadows:{shadows:1,notifications_refused:0,notifications_refused_recent:0,sync:[{name:'desired',conflicts:1,conflicts_recent:1,failures:3,consecutive_failures:3,last_success_ms:null,last_error:'network'}],omitted:0}};
const kinds=result({...healthy,services:service}).items.map(a=>a.kind);
for(const kind of ['messaging-backpressure','messaging-loss','bridge-disconnected','stream-near-limit','stream-data-lost','export-failing','shadow-sync-failing','shadow-conflict'])assert(kinds.includes(kind),kind);
service.messaging.queues[0].full_since_ms=now-119_999;service.messaging.queues[0].refused_recent=0;service.bridge.since_ms=now-119_999;service.bridge.refused_recent=0;
assert(!result({...healthy,services:service}).items.some(a=>['messaging-backpressure','bridge-disconnected'].includes(a.kind)),'sustained threshold is two sampling intervals');
assert.equal(result({...healthy,status:'unreported',services:service},[failed]).items.length,0);
console.log('Direct-chain alert parity and fail-closed recovery fixtures passed');
