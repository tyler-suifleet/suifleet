const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const catalog = JSON.parse(fs.readFileSync('interfaces/fleet-v1.json'));
const manifest = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
for (const group of ['customer','security_policy','security_delay']) for (const type of catalog.optional_type_origins[group]) manifest.type_origins[type]=manifest.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
const load=loader(), delay=load('lib/security-delay.ts');
const { bcs }=appRequire('@mysten/sui/bcs');
const C='0x'+'c'.padStart(64,'0'), A='0x'+'a'.padStart(64,'0');
assert.equal(delay.delaySupported(),true);
const partial=structuredClone(manifest);delete partial.type_origins[catalog.optional_type_origins.security_delay[0]];
assert.throws(()=>load('lib/deployment-manifest.ts').parseManifest(JSON.stringify(partial)),/partial security delay/);
assert.deepEqual(delay.validateDelays(Array(7).fill(1000)),Array(7).fill(1000));
for(const values of [[],Array(6).fill(1000),[999,...Array(6).fill(1000)],Array(7).fill(518400001),Array(7).fill(NaN),Array(7).fill(1000.5)]) assert.throws(()=>delay.validateDelays(values));
const edit={operation:'set_security',values:[8,0,0,0,0,0,0]};
const args=delay.editArguments(new Uint8Array([1,2,3]),edit);
assert.deepEqual(bcs.vector(bcs.vector(bcs.u8())).parse(args),[Array.from(bcs.string().serialize('set_security').toBytes()),[1,2,3],[7,8,0,0,0,0,0,0]]);
const queue=delay.buildQueueSecurityTx(C,args,100000n).getData();
assert.equal(queue.commands[0].MoveCall.function,'queue_security_action');
assert.equal(queue.commands[0].MoveCall.arguments.length,4);
assert.equal(delay.buildCancelSecurityTx(C,A).getData().commands[0].MoveCall.function,'cancel_security_action');
assert.equal(delay.buildExecuteSecurityEditTx(C,A,edit).getData().commands[0].MoveCall.function,'set_security_secured');
assert.equal(delay.buildDelayConfigurationTx(C,Array(7).fill(1000)).getData().commands[0].MoveCall.function,'configure_security_delays');
assert.throws(()=>delay.buildQueueSecurityTx(C,new Uint8Array(65537),1n),/64 KiB/);
const action={id:A,proposer:C,generation:1n,expires:10000n,created:0n,intent:[],edit,readyAt:1000n,stale:false};
assert.match(delay.delayStatus(action,999n),/Ready in 1 seconds/);
assert.equal(delay.delayStatus(action,1000n),'Ready to execute');
assert.equal(delay.delayStatus(action,10000n),'Expired');
assert.match(delay.delayStatus({...action,stale:true},0n),/Invalidated/);
assert.match(delay.delayStatus({...action,readyAt:undefined},0n),/unavailable/);
assert.notDeepEqual(delay.securityIntent(C,args),delay.securityIntent(A,args));
// Render the production console: operators see countdown, cancel, queued values
// and disabled early execution, using real React rather than source-string checks.
const React=appRequire('react'), {renderToStaticMarkup}=appRequire('react-dom/server');
const now=BigInt(Date.now());
const fakeQueue=[{...action,readyAt:now+60000n,expires:now+70000n}];
const view=loader({
 '@tanstack/react-query':{useQuery:({queryKey})=>({data:queryKey[0]==='organization-security'?{generation:1n,requirements:Array(7).fill(8)}:queryKey[0]==='organization-delays'?Array(7).fill(1000):fakeQueue,isSuccess:true,isPending:false,error:null,refetch:async()=>{}})},
 '@mysten/dapp-kit-react':{useCurrentClient:()=>({}),useCurrentAccount:()=>({address:C})},
 '@/hooks/useFleetSelection':{useFleetSelection:()=>({fleet:{customerId:C,customerName:'Test org',administrator:true}})},
 '@/hooks/useFleetTransaction':{useFleetTransaction:()=>({isPending:false,mutateAsync:async()=>{}})},
})("components/OrganizationSecurity.tsx");
const html=renderToStaticMarkup(React.createElement(view.OrganizationSecurity));
assert.match(html,/Ready in 60 seconds/);assert.match(html,/Cancel action/);assert.match(html,/disabled="">Execute action/);assert.match(html,/Queue protection changes/);
console.log('PASS delay bounds, canonical args, domain isolation, SDK queue/configure/cancel/execute, countdown/expiry/stale, production console controls');
(async()=>{
 const {manifestClient}=require('./frontend_test_utils.cjs');
 const {ObjectError}=appRequire('@mysten/sui/client');
 const reader=loader({'./event-history':{incrementalEvents:async()=>[{bcs:event}]}});
 const wire=reader('lib/fleet-protocol.ts'), api=reader('lib/security-delay.ts');
 const customerBytes=wire.customer_CustomerBcs.serialize({id:C,version:'197',registry_id:manifest.shared_objects.registry.id,name:'Org',credentials:{contents:[]},administrators:'1',fleets:[],default_fleet:null,active_devices:'0',device_limit:'0',allowance:'0',used:'0',period:'0',charge_type:[]}).toBytes();
 const canonical=api.editArguments(customerBytes,edit);
 const intent=api.securityIntent(C,canonical);
 const event=wire.security_policy_ActionCreatedBcs.serialize({action_id:A,customer_id:C,generation:'1',proposer:C,intent,expires_ms:'10000'}).toBytes();
 const actionBytes=wire.security_policy_ActionBcs.serialize({id:A,customer_id:C,generation:'1',proposer:C,intent,expires_ms:'10000',attested:0}).toBytes();
 const client=manifestClient(manifest),original=client.getObject;
 client.getObject=async request=>request.objectId===C?{object:{type:manifest.call_package_id+'::customer::Customer',content:customerBytes}}:original(request);
 let mode='valid';
 client.getObjects=async()=>({objects:mode==='deleted'?[new ObjectError('gone','deleted',{reason:'deleted',objectId:A})]:mode==='transport'?[new Error('transport failed')]:[{type:manifest.call_package_id+'::security_policy::Action',content:actionBytes}]});
 client.getDynamicField=async({name})=>{
  let type,bytes;
  if(name.type.endsWith('::DelayKey')) {type='Delays';bytes=wire.security_policy_DelaysBcs.serialize({milliseconds:Array(7).fill('1000')}).toBytes()}
  else if(name.type.endsWith('::QueuedAtKey')) {if(mode==='legacy')throw new ObjectError('missing','missing',{reason:'notFound'});type='QueuedAt';bytes=wire.security_policy_QueuedAtBcs.serialize({milliseconds:'0'}).toBytes()}
  else {type='ActionArguments';bytes=wire.security_policy_ActionArgumentsBcs.serialize({bytes:mode==='tampered'?[0]:Array.from(canonical)}).toBytes()}
  return {dynamicField:{value:{type:manifest.call_package_id+'::security_policy::'+type,bcs:bytes}}};
 };
 assert.deepEqual(await api.fetchSecurityDelays(client,C),Array(7).fill(1000));
 let rows=await api.fetchQueuedSecurityActions(client,C,1n,Array(7).fill(8),Array(7).fill(1000));
 assert.equal(rows[0].readyAt,1000n);assert.deepEqual(rows[0].edit,edit);assert.equal(rows[0].stale,false);
 rows=await api.fetchQueuedSecurityActions(client,C,2n,Array(7).fill(8),Array(7).fill(1000));assert.equal(rows[0].stale,true);
 mode='legacy';rows=await api.fetchQueuedSecurityActions(client,C,1n,Array(7).fill(8),Array(7).fill(1000));assert.equal(rows[0].readyAt,undefined);
 mode='deleted';assert.deepEqual(await api.fetchQueuedSecurityActions(client,C,1n,Array(7).fill(8),Array(7).fill(1000)),[]);
 mode='transport';await assert.rejects(api.fetchQueuedSecurityActions(client,C,1n,Array(7).fill(8),Array(7).fill(1000)),/transport failed/);
 mode='tampered';await assert.rejects(api.fetchQueuedSecurityActions(client,C,1n,Array(7).fill(8),Array(7).fill(1000)),/intent mismatch/);
 console.log('PASS authoritative queue/delay BCS readers, canonical payload hash, generation invalidation, legacy missing timestamp, consumed nonce and transport/tamper rejection');
})().catch(e=>{console.error(e);process.exitCode=1});
