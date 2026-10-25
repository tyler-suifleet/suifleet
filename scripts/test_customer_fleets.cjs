// Offline production-code tests: verified discovery, stale credentials,
// account/chain preference boundaries and real SDK PTB capability composition.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { loader, manifestClient, appRequire } = require("./frontend_test_utils.cjs");
const protocol = JSON.parse(fs.readFileSync("interfaces/fleet-v1.json"));
const m = JSON.parse(fs.readFileSync("manifests/fixture.localnet.json"));
for (const name of [...protocol.optional_type_origins.customer, ...protocol.optional_type_origins.fleet_settings]) m.type_origins[name] = m.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader();
const fleets = load("lib/customer-fleets.ts");
const wire = load("lib/fleet-protocol.ts");
const settings = load("lib/fleet-settings.ts");
const { normalizeSuiAddress: id } = appRequire("@mysten/sui/utils");
const A=id("0xa"), B=id("0xb"), C=id("0xc"), F=id("0xf"), G=id("0x10"), BUD=id("0xb0"), CAP=id("0xc0");
const base = { id:F, customerId:C, customerName:"Customer", name:"Production", budgetId:BUD,
  devices:[],groups:[],deployments:[],policies:[],enrollmentPolicies:[],credentials:[],administrator:true,isDefault:true,activeDevices:0n,deviceLimit:2n,allowance:10n,used:0n };
assert.equal(fleets.selectFleet([base], null), base);
assert.equal(fleets.selectFleet([base], G), undefined, "stale preference cannot substitute a fleet");
assert.notEqual(fleets.selectionKey(A), fleets.selectionKey(B));
const tx = settings.buildSetFleetSettingsTx(BUD,CAP,settings.DEFAULT_SETTINGS);
const wrapped = fleets.withManagedBudgetCap(tx,base,CAP).getData();
assert.deepEqual(wrapped.commands.map(c=>c.MoveCall.function),["borrow_budget_cap","set","return_budget_cap"]);
assert.deepEqual(wrapped.commands[1].MoveCall.arguments[1].NestedResult,[0,0]);
assert.deepEqual(wrapped.commands[2].MoveCall.arguments.slice(1).map(a=>a.NestedResult),[[0,0],[0,1]]);
assert.ok(wrapped.inputs.every(i=>i.UnresolvedObject?.objectId!==CAP),"custodied cap must not remain a transaction input");
assert.equal(fleets.withManagedBudgetCap(tx,base,id("0x99")),tx,"unrelated transaction stays unchanged");
const archive = load("lib/fleet-bulk.ts");
const approval = archive.buildArchiveApprovalTx({ packageId: m.call_package_id, fleetId: id("0xf0"), budgetId: BUD }, new Uint8Array(56),
  { kind: "owner", ownerCapId: CAP, managed: { customerId: C, fleetId: F } }).getData();
assert.equal(approval.commands.length, 1);
assert.equal(approval.commands[0].MoveCall.function, "seal_approve_customer");
assert.equal(approval.commands[0].MoveCall.arguments.length, 5);
assert.ok(approval.inputs.every(i => i.UnresolvedObject?.objectId !== CAP), "Seal approval must not borrow a mutable capability");
const type=(name)=>`${m.call_package_id}::customer::${name}`;
let revoked=false, wrongCustomer=false;
const client=manifestClient(m), original=client.getObject;
client.listOwnedObjects=async()=>({objects:[{objectId:id("0x11"),content:wire.customer_MembershipBcs.serialize({id:id("0x11"),customer_id:C}).toBytes()}],hasNextPage:false});
client.getObject=async request=>{
  if(request.objectId===C)return {object:{objectId:C,type:type("Customer"),content:wire.customer_CustomerBcs.serialize({id:C,version:1,registry_id:m.shared_objects.registry.id,name:"Customer",credentials:{contents:revoked?[]:[{key:A,value:true}]},administrators:1,fleets:[F,G],default_fleet:F,active_devices:1,device_limit:2,allowance:10,used:6,period:1,charge_type:[]}).toBytes()}};
  if([F,G].includes(request.objectId))return {object:{objectId:request.objectId,type:type("Fleet"),content:wire.customer_FleetBcs.serialize({id:request.objectId,version:1,customer_id:wrongCustomer?B:C,name:"Production",budget_id:request.objectId===G?id("0xb1"):BUD,devices:request.objectId===F?[id("0xd")]:[],pending_devices:0,groups:[],deployments:[],policies:{contents:[]},enrollment_policies:[]}).toBytes()}};
  return original(request);
};
const opaque = loader()("lib/opaque-data.ts");
for (const [authority, fn, count] of [
 [{kind:"customer",customerId:C,fleetId:F},"seal_approve_scoped_customer",7],
 [{kind:"owner",ownerCapId:CAP},"seal_approve_scoped_owner",6],
 [{kind:"reader"},"seal_approve_scoped_reader",4],
]) {
 const scoped=opaque.buildScopedApprovalTx({packageId:m.call_package_id,fleetId:id("0xf0"),budgetId:BUD},new Uint8Array(120),id("0xd"),new Uint8Array(32),authority).getData();
 assert.equal(scoped.commands.length,1);
 assert.equal(scoped.commands[0].MoveCall.function,fn);
 assert.equal(scoped.commands[0].MoveCall.arguments.length,count);
 if(authority.kind==="customer") {
  assert.deepEqual(scoped.inputs.filter(i=>i.UnresolvedObject).map(i=>i.UnresolvedObject.objectId),[id("0xf0"),BUD,C,F]);
  assert.ok(scoped.inputs.every(i=>i.UnresolvedObject?.objectId!==CAP));
 }
}
(async()=>{
 const result=await fleets.fetchCustomerFleets(client,A);
 assert.equal(result.length,2);assert.equal(result[0].name,result[1].name,"aliases need not be unique");
 assert.equal(result[0].used,6n);
 revoked=true;assert.deepEqual(await fleets.fetchCustomerFleets(client,A),[],"retained membership cannot restore revoked authority");
 revoked=false;wrongCustomer=true;await assert.rejects(()=>fleets.fetchCustomerFleets(client,A),/customer mismatch/);
 // Explicit pre-existing support grants discover their fleet without a
 // customer Membership and authorize only the exact diagnostic request.
 wrongCustomer=false;revoked=false;
 const cfg={schema:1,chainIdentifier:m.chain_identifier,registryId:m.shared_objects.registry.id,packageId:id("0xf0"),typeOrigin:id("0xf0"),fleetId:id("0xf1"),budgetId:BUD,storageId:id("0xf2")};
 process.env.NEXT_PUBLIC_FLEET_DATA=JSON.stringify(cfg);
 const guestLoad=loader(), data=guestLoad("lib/fleet-data.ts"), guestRead=guestLoad("lib/customer-fleets.ts"), support=guestLoad("lib/fleet-support.ts");
 const guestClient=manifestClient(m), fallback=guestClient.getObject;let supportActive=true, readerActive=false;
 guestClient.listOwnedObjects=async()=>({objects:[],hasNextPage:false});
 guestClient.listEvents=async()=>({events:[{bcs:wire.customer_FleetCreatedBcs.serialize({customer_id:C,fleet_id:F,name:"Production",budget_id:BUD}).toBytes()}],hasNextPage:false});
 const W=id("0xfe"), D=id("0xd");
 guestClient.getObject=async request=>{
  if(request.objectId===cfg.packageId)return {object:{objectBcs:appRequire("@mysten/sui/bcs").bcs.Object.serialize({data:{Package:{id:cfg.packageId,version:1,moduleMap:new Map(),linkageTable:new Map(),typeOriginTable:[{moduleName:"fleet_data",datatypeName:"ReadGrantKey",package:cfg.typeOrigin}]}},owner:{Immutable:true},previousTransaction:appRequire("@mysten/sui/utils").toBase58(new Uint8Array(32)),storageRebate:0}).toBytes()}};
  if(request.objectId===cfg.fleetId)return {object:{objectId:cfg.fleetId,owner:{$kind:"Shared"},type:`${cfg.typeOrigin}::fleet_data::FleetData`,content:data.FleetDataBcs.serialize({id:cfg.fleetId,version:1,budget_id:BUD,storage_id:cfg.storageId,registry_id:cfg.registryId,chain_identifier:cfg.chainIdentifier,key_epoch:0,support:supportActive?[B]:[],health_interval_ms:3600000,batch_interval_ms:86400000,emergency_interval_ms:300000,device_count:1}).toBytes()}};
  if(request.objectId===W)return {object:{objectId:W,owner:{$kind:"Shared"},type:`${cfg.typeOrigin}::fleet_data::DeviceData`,content:data.DeviceDataBcs.serialize({id:W,version:1,fleet_id:cfg.fleetId,device_id:D,account_id:id("0xac"),health_seq:0,health_at_ms:0,message:[],signature:[],batch_seq:0,batch_at_ms:null,emergency_at_ms:null,anchor_count:0}).toBytes()}};
  return [C,F,G].includes(request.objectId)?client.getObject(request):fallback(request);
 };
 const bcs=appRequire("@mysten/sui/bcs").bcs;
 const grantKey=bcs.struct("ReadGrantKey",{device_id:bcs.Address,stream_hash:bcs.byteVector(),reader:bcs.Address});
 guestClient.listDynamicFields=async()=>({dynamicFields:readerActive?[{$kind:"DynamicField",name:{type:`${cfg.typeOrigin}::fleet_data::ReadGrantKey`,bcs:grantKey.serialize({device_id:D,stream_hash:[],reader:B}).toBytes()},valueType:"bool"}]:[],hasNextPage:false});
 guestClient.getDynamicField=async()=>({dynamicField:{value:{type:"0x2::object::ID",bcs:appRequire("@mysten/sui/bcs").bcs.Address.serialize(W).toBytes()}}});
 // A 10k-field device index must not block credentialed administrators.
 const savedOwned=guestClient.listOwnedObjects, savedList=guestClient.listDynamicFields;
 guestClient.listOwnedObjects=client.listOwnedObjects;
 let scans=0;
 guestClient.listDynamicFields=async()=>{scans++;throw Error("large device index must not be scanned");};
 let admins=await guestRead.fetchCustomerFleets(guestClient,A);
 assert.equal(admins.length,2);assert.equal(scans,0,"verified member bypasses grant index entirely");
 assert.deepEqual(guestRead.withManagedBudgetCap(tx,admins[0],CAP).getData().commands.map(c=>c.MoveCall.function),["borrow_budget_cap","set","return_budget_cap"]);
 // A separate configured locator may exhaust its bound without suppressing
 // the member's fleets or capability-loan transaction recheck.
 const other={...cfg,fleetId:id("0xf3"),budgetId:id("0xb3")};
 const savedGet=guestClient.getObject;
 guestClient.getObject=async request=>request.objectId===other.fleetId?{object:{objectId:other.fleetId,owner:{$kind:"Shared"},type:`${other.typeOrigin}::fleet_data::FleetData`,content:data.FleetDataBcs.serialize({id:other.fleetId,version:1,budget_id:other.budgetId,storage_id:other.storageId,registry_id:other.registryId,chain_identifier:other.chainIdentifier,key_epoch:0,support:[],health_interval_ms:3600000,batch_interval_ms:86400000,emergency_interval_ms:300000,device_count:10001}).toBytes()}}:savedGet(request);
 process.env.NEXT_PUBLIC_FLEET_DATA_CONFIGS=JSON.stringify([cfg,other]);
 const warnings=[];scans=0;
 guestClient.listDynamicFields=async()=>({dynamicFields:Array.from({length:100},()=>({name:{type:`${cfg.typeOrigin}::fleet_data::DeviceKey`}})),hasNextPage:true,cursor:`page-${++scans}`});
 admins=await guestRead.fetchCustomerFleets(guestClient,A,message=>warnings.push(message));
 assert.equal(admins.length,2);assert.equal(admins[0].administrator,true);
 assert.equal(scans,100);assert.equal(warnings.length,1);assert.match(warnings[0],/discovery unavailable/);
 assert.equal(guestRead.withManagedBudgetCap(tx,admins[0],CAP).getData().commands[0].MoveCall.function,"borrow_budget_cap");
 // Exercise the shipped transaction hook: fresh discovery still reaches the
 // fixture wallet and confirms node effects despite the unrelated full index.
 let submitted;
 const transactionSelection={supported:true,fleet:admins[0],identity:"admin"};
 const transactionLoad=loader({
  "react":{useRef:value=>({current:value})},
  "@tanstack/react-query":{useMutation:options=>options,useQueryClient:()=>({invalidateQueries:async()=>{}})},
  "@mysten/enoki":{isEnokiWallet:()=>false},
  "@mysten/dapp-kit-react":{useCurrentClient:()=>guestClient,useDAppKit:()=>({stores:{$connection:{get:()=>({account:{address:A,chains:[`sui:${m.network}`]}})},$currentNetwork:{get:()=>m.network}},signAndExecuteTransaction:async({transaction})=>{submitted=transaction;return {Transaction:{status:{success:true},digest:"fixture-digest"}};}})},
  "./useFleetSelection":{useFleetSelection:()=>transactionSelection},
  "@/lib/customer-fleets":guestRead,
  "@/lib/fleet-budget":{fetchManagedBudget:async()=>({capId:CAP})},
 });
 const oldStorage=global.localStorage,oldWarn=console.warn;
 global.localStorage={getItem:()=>F};console.warn=()=>{};
 guestClient.waitForTransaction=async()=>({Transaction:{status:{success:true},digest:"fixture-digest",events:[]}});
 try {
  const executed=await transactionLoad("hooks/useFleetTransaction.ts").useFleetTransaction().mutationFn({transaction:tx});
  assert.equal(executed.digest,"fixture-digest");
  assert.equal(submitted.getData().commands[0].MoveCall.function,"borrow_budget_cap");
 } finally {global.localStorage=oldStorage;console.warn=oldWarn;}
 guestClient.listDynamicFields=async()=>{throw Error("RPC unavailable");};
 assert.equal((await guestRead.fetchCustomerFleets(guestClient,A,message=>warnings.push(message))).length,2);
 assert.equal(warnings.length,2,"RPC errors are contained and reported");
 guestClient.listOwnedObjects=savedOwned;
 assert.deepEqual(await guestRead.fetchCustomerFleets(guestClient,A,()=>{}),[],"failed reader discovery confers no authority");
 delete process.env.NEXT_PUBLIC_FLEET_DATA_CONFIGS;
 guestClient.getObject=savedGet;guestClient.listDynamicFields=savedList;
 const guest=await guestRead.fetchCustomerFleets(guestClient,B);assert.equal(guest.length,1);assert.equal(guest[0].support,true);assert.equal(guest[0].administrator,false);
 const {Transaction}=appRequire("@mysten/sui/transactions");
 const diagnostic=new Transaction();diagnostic.moveCall({target:`${cfg.packageId}::fleet_data::request_diagnostics_support`,arguments:[diagnostic.object(cfg.fleetId),diagnostic.object(W),diagnostic.object("0x6")]});
 await support.verifySupportDiagnostics(guestClient,guest[0],cfg,D,B,diagnostic);
 const extra=Transaction.from(diagnostic);extra.transferObjects([extra.gas],B);
 await assert.rejects(()=>support.verifySupportDiagnostics(guestClient,guest[0],cfg,D,B,extra),/single diagnostic request/);
 const unused=Transaction.from(diagnostic);unused.object(id("0xff"));
 await assert.rejects(()=>support.verifySupportDiagnostics(guestClient,guest[0],cfg,D,B,unused),/single diagnostic request/);
 const substituted=new Transaction();substituted.moveCall({target:`${cfg.packageId}::fleet_data::request_diagnostics_support`,arguments:[substituted.object(cfg.fleetId),substituted.object(id("0xff")),substituted.object("0x6")]});
 await assert.rejects(()=>support.verifySupportDiagnostics(guestClient,guest[0],cfg,D,B,substituted),/binding changed/);
 supportActive=false;assert.deepEqual(await guestRead.fetchCustomerFleets(guestClient,B),[]);
 await assert.rejects(()=>support.verifySupportDiagnostics(guestClient,guest[0],cfg,D,B,diagnostic),/revoked/);
 readerActive=true;
 const listGrants=guestClient.listDynamicFields,readField=guestClient.getDynamicField;
 guestClient.listDynamicFields=async({cursor})=>cursor?listGrants():{dynamicFields:[],hasNextPage:true,cursor:"next"};
 assert.equal(await guestLoad("lib/opaque-data.ts").hasScopedReadGrant(guestClient,cfg,B),true,"discovery follows grant pages");
 guestClient.listDynamicFields=async()=>({dynamicFields:[],hasNextPage:true,cursor:"repeat"});
 await assert.rejects(()=>guestLoad("lib/opaque-data.ts").hasScopedReadGrant(guestClient,cfg,B),/Invalid read grant page/);
 guestClient.listDynamicFields=listGrants;
 guestClient.getDynamicField=async()=>{throw new (appRequire("@mysten/sui/client").ObjectError)("NOT_FOUND","revoked",{reason:"notFound"});};
 assert.equal(await guestLoad("lib/opaque-data.ts").hasScopedReadGrant(guestClient,cfg,B),false,"stale index cannot restore revoked grant");
 guestClient.getDynamicField=readField;
 const reader=await guestRead.fetchCustomerFleets(guestClient,B);
 assert.equal(reader.length,1,"non-member scoped grantee discovers granting fleet");
 assert.equal(reader[0].administrator,false);assert.equal(reader[0].support,false);
 const authority=guestLoad("lib/data-authority.ts");
 assert.equal(authority.selectedDataAuthority(reader,reader[0],BUD).kind,"reader");
 assert.deepEqual(await guestRead.fetchCustomerFleets(guestClient,A),[],"another wallet cannot use the grant");
 // Render the shipped FleetDataBrowser with a customer-enabled selection.
 // Keep discovery and authority real; fixture only wallet/provider/storage I/O.
 const React=appRequire("react"),{renderToString}=appRequire("react-dom/server");
 const fixture=await require("./fixtures/data-browser.cjs")(),row=fixture.fixtures[0].row;
 let adapter,approved,fetches=0;
 const selection={supported:true,fleet:reader[0],identity:"grantee"};
 const selectionHook={useFleetSelection:()=>selection};
 const opaqueReal=guestLoad("lib/opaque-data.ts");
 const browserLoad=loader({
  "@mysten/dapp-kit-react":{useCurrentClient:()=>guestClient,useCurrentAccount:()=>({address:B}),useCurrentNetwork:()=>m.network,useDAppKit:()=>({stores:{$connection:{get:()=>({account:{address:B,chains:[`sui:${m.network}`]}})},$currentNetwork:{get:()=>m.network}},signPersonalMessage:async()=>({signature:"fixture"})})},
  "@mysten/seal":{SessionKey:{create:async()=>({isExpired:()=>false,getPersonalMessage:()=>new Uint8Array(),setPersonalMessageSignature:async()=>{}})}},
  "@/hooks/useDevices":{useDevices:()=>({data:[]})},"@/hooks/useGroups":{useGroups:()=>({data:[]})},
  "@/hooks/useFleetTransaction":{useFleetTransaction:()=>({mutateAsync:async()=>{throw Error("reader must not mutate");}})},
  "@/hooks/useFleetSelection":selectionHook,"./useFleetSelection":selectionHook,
  "@/lib/customer-fleets":guestRead,"@/lib/seal":{SEAL_SETTINGS:{}},
  "@/lib/opaque-data":{...opaqueReal,readDataItem:async()=>row.value,scopedDecryptor:async(...args)=>{approved=args[6];return ()=>{};},fetchDataItem:async()=>{fetches++;return new TextEncoder().encode("verified fixture plaintext");}},
  "./DataBrowser":{DataBrowser:props=>{adapter=props.adapter;return React.createElement("p",null,"Data browser reachable");}},
 });
 guestClient.$extend=()=>({walrus:{}});
 const Component=browserLoad("components/FleetDataBrowser.tsx").FleetDataBrowser;
 assert.match(renderToString(React.createElement(Component)),/Data browser reachable/);
 const opened=await adapter.open(row);
 assert.match(new TextDecoder().decode(opened),/fixture plaintext/);
 assert.equal(approved.kind,"reader");assert.equal(fetches,1);
 readerActive=false;
 await assert.rejects(()=>adapter.open(row),/revoked/);
 assert.equal(fetches,1,"revoked reader must be refused before fetching plaintext");
 assert.throws(()=>authority.selectedDataAuthority([],reader[0],BUD),/revoked/);
 assert.deepEqual(await guestRead.fetchCustomerFleets(guestClient,B),[],"grant revocation removes discovery without membership");
 console.log("PASS: multi-fleet discovery, aliases, revoked credentials, isolation, capability loans, read-only Seal approval and independent support/reader authority, pagination, stale grants and FleetDataBrowser revocation");

})().catch(error=>{console.error(error);process.exitCode=1;});
