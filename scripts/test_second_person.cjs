const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader, manifestClient, appRequire } = require('./frontend_test_utils.cjs');
const m = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
const catalog = JSON.parse(fs.readFileSync('interfaces/fleet-v1.json'));
for (const group of ['customer', 'security_policy', 'second_person', 'passkey_factor', 'security_delay']) for (const type of catalog.optional_type_origins[group]) m.type_origins[type] = m.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), approvals = load('lib/second-person.ts'), wire = load('lib/fleet-protocol.ts'), security = load('lib/security-policy.ts');
const { bcs } = appRequire('@mysten/sui/bcs');
const id = n => '0x' + n.toString(16).padStart(64,'0');
const C=id(10), A=id(11), B=id(12), R=id(13), consent=id(14);
const customer = {id:C,version:'199',registry_id:m.shared_objects.registry.id,name:'Org',credentials:{contents:[{key:A,value:true},{key:B,value:true}]},administrators:'2',fleets:[],default_fleet:null,active_devices:'0',device_limit:'0',allowance:'0',used:'0',period:'0',charge_type:[]};
const content = wire.customer_CustomerBcs.serialize(customer).toBytes();
const args = [Array.from(bcs.string().serialize('set_security').toBytes()),Array.from(bcs.Address.serialize(C).toBytes()),Array.from(bcs.vector(bcs.u8()).serialize([0,0,0,0,0,0,0]).toBytes())];
const bytes = bcs.vector(bcs.vector(bcs.u8())).serialize(args).toBytes();
const action = {id:R,customer_id:C,generation:'8',proposer:A,intent:Array(32).fill(0),expires_ms:'9999999999999',attested:4};
const encoded = wire.security_policy_ActionBcs.serialize(action).toBytes();
assert.equal(security.AVAILABLE_SECURITY_FACTORS,15);
assert.equal(approvals.secondPersonSupported(),true);
assert.deepEqual(security.validateSecurityRequirements([4,0,0,0,0,0,0]),[4,0,0,0,0,0,0]);
assert.deepEqual(security.validateSecurityRequirements([6,0,0,0,0,0,0]),[6,0,0,0,0,0,0]);
assert.deepEqual(security.validateSecurityRequirements([8,0,0,0,0,0,0]),[8,0,0,0,0,0,0]);
assert.deepEqual(approvals.approvalRequirements([2,3,6,7,2,1,0],[4,0,4,0,4,4,0]),[6,3,6,3,6,5,0]);
const delay = load('lib/security-delay.ts');
assert.equal(delay.buildQueueSecurityTx(C,bytes,100n).getData().commands[0].MoveCall.function,'propose_approval');
assert.equal(delay.buildCancelSecurityTx(C,R).getData().commands[0].MoveCall.function,'cancel_disclosed_security_action');
assert.throws(()=>delay.buildQueueSecurityTx(C,new Uint8Array(16385),100n),/16 KiB/);
const request = approvals.decodeRequest(R,C,encoded,bytes);
assert.equal(request.operation,'set_security'); assert.equal(request.proposer,A); assert.equal(request.approved,true);
assert.deepEqual(approvals.policyValues(request),[0,0,0,0,0,0,0]);
assert.throws(()=>approvals.decodeRequest(R,id(999),encoded,bytes),/another organization/);
for (const decision of ['approve','reject','cancel']) {
 const command = approvals.decisionTx(C,R,decision).getData().commands[0].MoveCall;
 assert.equal(command.function,decision==='cancel'?'cancel_disclosed_security_action':decision+'_action');
 assert.equal(command.arguments.length,decision==='approve'?3:2);
}

assert.equal(approvals.configureTx(C,consent).getData().commands[0].MoveCall.function,'configure_second_person');
(async()=>{
 const client = manifestClient(m), original = client.getObject;
 let changed=false, malformed=false, unavailable=false, page=0, requirements=[0,0,0,0,4,0,0];
 client.getObject = async input => input.objectId===C ? {object:{type:`${m.call_package_id}::customer::Customer`,content:changed?wire.customer_CustomerBcs.serialize({...customer,name:'Changed'}).toBytes():content}} : input.objectId===R ? {object:{type:`${m.call_package_id}::security_policy::Action`,content:encoded}} : original(input);
 client.listDynamicFields=async()=> ++page%2 ? {dynamicFields:[],hasNextPage:true,cursor:'next'} : {dynamicFields:[{name:{type:`${m.call_package_id}::security_policy::FactorKey`,bcs:Uint8Array.of(0)}}],hasNextPage:false};
 client.getDynamicField=async input=>{
 const type=input.name.type.split('::').at(-1);
 if(type==='FactorKey' && input.parentId===C)return {dynamicField:{value:{type:malformed?'vector<u8>':`vector<${'0x'+'0'.repeat(63)+'2'}::object::ID>`,bcs:bcs.vector(bcs.Address).serialize(unavailable?[id(99),R]:[R]).toBytes()}}};
 if(type==='FactorKey' && input.parentId===R)return {dynamicField:{value:{type:`${m.call_package_id}::security_policy::FactorConfiguration`,bcs:wire.security_policy_FactorConfigurationBcs.serialize({binding:Array.from(bytes)}).toBytes()}}};
 return {dynamicField:{value:{type:`${m.call_package_id}::security_policy::Policy`,bcs:wire.security_policy_PolicyBcs.serialize({schema:'1',generation:'8',requirements,transaction:[],attested:0}).toBytes()}}};
 };
 assert.deepEqual(await approvals.fetchRequests(client,C),[request]);
 unavailable=true;
 const partial=await approvals.fetchRequests(client,C);
 assert.equal(partial[0].id,id(99)); assert.equal(typeof partial[0].error,'string');
 assert.deepEqual(partial[1],request); unavailable=false;
 malformed=true;await assert.rejects(approvals.fetchRequests(client,C),/pending index type/);malformed=false;
 assert.equal((await approvals.proposePolicyTx(client,C,[0,0,0,0,0,0,0],100n)).getData().commands[0].MoveCall.function,'propose_approval');
 assert.equal((await approvals.executePolicyTx(client,C,request)).getData().commands[0].MoveCall.function,'set_security_secured');
 const combined=[2,3,6,7,6,5,0];
 const proposed=await approvals.policyChangeTx(client,C,combined);
 assert.equal(proposed.getData().commands[0].MoveCall.function,'propose_approval');
 const combinedRequest={...request,arguments:[args[0],args[1],Array.from(bcs.vector(bcs.u8()).serialize(combined).toBytes())]};
 assert.deepEqual(approvals.policyValues(combinedRequest),combined);
 assert.equal((await approvals.executePolicyTx(client,C,combinedRequest)).getData().commands[0].MoveCall.function,'set_security_secured');
 requirements=[2,2,2,2,2,2,2];
 assert.equal((await approvals.policyChangeTx(client,C,combined)).getData().commands[0].MoveCall.function,'set_security');
 // Reducing any protected action requires its approval, even if action 4 does not.
 requirements=[4,0,0,0,0,0,0];
 assert.equal((await approvals.policyChangeTx(client,C,[0,0,0,0,0,0,0])).getData().commands[0].MoveCall.function,'propose_approval');
 // Removing passkeys also needs approval on a combined-factor reduced action.
 requirements=[6,2,2,2,2,2,2];
 assert.equal((await approvals.policyChangeTx(client,C,requirements.map(v=>v & ~2))).getData().commands[0].MoveCall.function,'propose_approval');
 // Delay-only and combined passkey/delay reductions must also queue.
 requirements=[10,2,2,2,2,2,2];
 assert.equal((await approvals.policyChangeTx(client,C,requirements.map(v=>v & ~2))).getData().commands[0].MoveCall.function,'propose_approval');
 requirements=[0,0,0,0,8,0,0];
 assert.equal((await approvals.policyChangeTx(client,C,Array(7).fill(2))).getData().commands[0].MoveCall.function,'propose_approval');
 // Keeping/strengthening approval-only actions does not require their approval.
 requirements=[4,0,0,0,0,0,0];
 assert.equal((await approvals.policyChangeTx(client,C,[6,2,2,2,2,2,2])).getData().commands[0].MoveCall.function,'set_security');
 requirements=[0,0,0,0,4,0,0];

 const delayedEdit={...request,operation:'configure_security_delays',arguments:[Array.from(bcs.string().serialize('configure_security_delays').toBytes()),args[1],Array.from(bcs.vector(bcs.u64()).serialize(Array(7).fill(1000)).toBytes())]};
 assert.equal((await approvals.executePolicyTx(client,C,delayedEdit)).getData().commands[0].MoveCall.function,'configure_security_delays_secured');
 await assert.rejects(approvals.executePolicyTx(client,C,{...request,generation:7n}),/missing or stale/);
 await assert.rejects(approvals.executePolicyTx(client,C,{...request,approved:false}),/missing or stale/);
 changed=true;assert.equal((await approvals.executePolicyTx(client,C,request)).getData().commands[0].MoveCall.function,'set_security_secured');changed=false;
 await assert.rejects(approvals.executePolicyTx(client,C,{...request,arguments:[args[0],Array.from(bcs.Address.serialize(id(999)).toBytes()),args[2]]}),/changed; propose again/);
 const intent=delay.securityIntent(C,bytes);
 const created=wire.security_policy_ActionCreatedBcs.serialize({action_id:R,customer_id:C,generation:'8',proposer:A,intent,expires_ms:action.expires_ms}).toBytes();
 const delayedReader=loader({'./event-history':{incrementalEvents:async()=>[{bcs:created}]}})('lib/security-delay.ts');
 const dc=manifestClient(m), get=dc.getObject;
 const {ObjectError}=appRequire('@mysten/sui/client');
 dc.getObject=async input=>input.objectId===C?{object:{type:`${m.call_package_id}::customer::Customer`,content}}:get(input);
 dc.getObjects=async()=>({objects:[{type:`${m.call_package_id}::security_policy::Action`,content:wire.security_policy_ActionBcs.serialize({...action,intent}).toBytes()}]});
 let corrupt=false;
 dc.getDynamicField=async({name})=>{
  if(name.type.endsWith('::ArgumentsKey'))throw new ObjectError('missing','missing',{reason:'notFound'});
  if(name.type.endsWith('::QueuedAtKey'))return {dynamicField:{value:{type:`${m.call_package_id}::security_policy::QueuedAt`,bcs:wire.security_policy_QueuedAtBcs.serialize({milliseconds:'0'}).toBytes()}}};
  return {dynamicField:{value:{type:`${m.call_package_id}::security_policy::FactorConfiguration`,bcs:wire.security_policy_FactorConfigurationBcs.serialize({binding:corrupt?[0]:Array.from(bytes)}).toBytes()}}};
 };
 const rows=await delayedReader.fetchQueuedSecurityActions(dc,C,8n,Array(7).fill(12),Array(7).fill(1000));
 assert.equal(rows[0].readyAt,1000n);assert.equal(rows[0].edit.operation,'set_security');assert.equal(rows[0].stale,false);
 corrupt=true;await assert.rejects(delayedReader.fetchQueuedSecurityActions(dc,C,8n,Array(7).fill(12),Array(7).fill(1000)),/intent mismatch/);
 const React = appRequire('react'), {renderToStaticMarkup} = appRequire('react-dom/server');
 let account=A, uiRequest={...request,approved:false}, uiError=null;
 const uiLoad=loader({
  '@mysten/dapp-kit-react':{useCurrentClient:()=>client},
  '@tanstack/react-query':{useQuery:()=>({data:{requests:[uiRequest],policy:{generation:8n}},error:uiError})},
  '@/hooks/useCurrentAccount':{useCurrentAccount:()=>({address:account})},
  '@/hooks/useFleetSelection':{useFleetSelection:()=>({identity:'test',fleet:{customerId:C,administrator:true}})},
  '@/hooks/useFleetTransaction':{useFleetTransaction:()=>({isPending:false})},
 });
 const View=uiLoad('components/SecondPersonApprovals.tsx').SecondPersonApprovals;
 let html=renderToStaticMarkup(React.createElement(View));
 assert.match(html,/Pending approvals/);assert.match(html,/set_security/);assert.match(html,/Expires/);assert.doesNotMatch(html,/>Approve exact action</);
 uiRequest={...request,arguments:[args[0],args[1],Array.from(bcs.vector(bcs.u8()).serialize([7,6,2,1,4,0,0]).toBytes())]};
 html=renderToStaticMarkup(React.createElement(View));
 assert.match(html,/Multisig administrators \+ Passkey co-signature \+ Second-person approval/);
 assert.match(html,/Passkey co-signature \+ Second-person approval/);
 account=B;html=renderToStaticMarkup(React.createElement(View));assert.match(html,/>Approve exact action</);assert.match(html,/>Reject</);
 uiRequest={...request,expires:1n};html=renderToStaticMarkup(React.createElement(View));assert.match(html,/Expired/);assert.match(html,/<button disabled="">Approve exact action/);
 uiRequest={id:id(99),error:'Object deleted'};
 html=renderToStaticMarkup(React.createElement(View));assert.match(html,/Object deleted/);assert.doesNotMatch(html,/>Approve exact action</);
 uiError=Error('connection unavailable');html=renderToStaticMarkup(React.createElement(View));assert.match(html,/connection unavailable/);

 let submitted;
 const setupLoad=loader({
  'react':{...React,useState:initial=>[initial,()=>{}]},
  '@mysten/dapp-kit-react':{useCurrentClient:()=>client},
  '@tanstack/react-query':{useQuery:()=>({data:{requirements:[6,3,2,2,6,2,2]}}),useQueryClient:()=>({invalidateQueries:async()=>{}})},
  '@/hooks/useFleetSelection':{useFleetSelection:()=>({fleet:{customerId:C,administrator:true}})},
  '@/hooks/useFleetTransaction':{useFleetTransaction:()=>({isPending:false,mutateAsync:async({transaction})=>{submitted=transaction;return {digest:'fixture'};}})},
 });
 const Setup=setupLoad('components/SecondPersonSetup.tsx').SecondPersonSetup;
 const tree=Setup();
 function elements(node) { return !node || typeof node!=='object' ? [] : [node,...[node.props?.children].flat(Infinity).flatMap(elements)]; }
 const boxes=elements(tree).filter(e=>e.type==='input');
 assert.deepEqual(boxes.map(e=>e.props.checked),[true,false,false,false,true,false,false]);
 // The current chain policy is read again at submission; preserve its other bits.
 requirements=[2,3,2,2,6,2,2];
 await elements(tree).find(e=>e.type==='button' && e.props.children==='Save or request policy change').props.onClick();
 assert.equal(submitted.getData().commands[0].MoveCall.function,'propose_approval');
 const encodedProposal=submitted.getData().inputs[1].Pure.bytes;
 const proposalBytes=bcs.vector(bcs.u8()).parse(Buffer.from(encodedProposal,'base64'));
 const proposalArgs=bcs.vector(bcs.vector(bcs.u8())).parse(Uint8Array.from(proposalBytes));
 assert.deepEqual(bcs.vector(bcs.u8()).parse(Uint8Array.from(proposalArgs[2])),[6,3,2,2,6,2,2]);
 client.ledgerService.getServiceInfo=async()=>({response:{chainId:'11111111111111111111111111111111'}});
 await assert.rejects(approvals.fetchRequests(client,C),/chain identifier mismatch/);
 console.log('PASS second-person console: SDK calls, split manifest origins, paged current index, wire/tenant/type checks, exact policy proposal/execution, stable identity, tampered identity and network refusal, proposer/approver/expiry/error rendering');
})().catch(e=>{console.error(e);process.exitCode=1});
