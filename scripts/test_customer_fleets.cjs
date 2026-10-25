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
 const guestClient=manifestClient(m), fallback=guestClient.getObject;let supportActive=true;
 guestClient.listOwnedObjects=async()=>({objects:[],hasNextPage:false});
 guestClient.listEvents=async()=>({events:[{bcs:wire.customer_FleetCreatedBcs.serialize({customer_id:C,fleet_id:F,name:"Production",budget_id:BUD}).toBytes()}],hasNextPage:false});
 const W=id("0xfe"), D=id("0xd");
 guestClient.getObject=async request=>{
  if(request.objectId===cfg.fleetId)return {object:{objectId:cfg.fleetId,owner:{$kind:"Shared"},type:`${cfg.typeOrigin}::fleet_data::FleetData`,content:data.FleetDataBcs.serialize({id:cfg.fleetId,version:1,budget_id:BUD,storage_id:cfg.storageId,registry_id:cfg.registryId,chain_identifier:cfg.chainIdentifier,key_epoch:0,support:supportActive?[B]:[],health_interval_ms:3600000,batch_interval_ms:86400000,emergency_interval_ms:300000,device_count:1}).toBytes()}};
  if(request.objectId===W)return {object:{objectId:W,owner:{$kind:"Shared"},type:`${cfg.typeOrigin}::fleet_data::DeviceData`,content:data.DeviceDataBcs.serialize({id:W,version:1,fleet_id:cfg.fleetId,device_id:D,account_id:id("0xac"),health_seq:0,health_at_ms:0,message:[],signature:[],batch_seq:0,batch_at_ms:null,emergency_at_ms:null,anchor_count:0}).toBytes()}};
  return [C,F,G].includes(request.objectId)?client.getObject(request):fallback(request);
 };
 guestClient.getDynamicField=async()=>({dynamicField:{value:{type:"0x2::object::ID",bcs:appRequire("@mysten/sui/bcs").bcs.Address.serialize(W).toBytes()}}});
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
 console.log("PASS: multi-fleet discovery, aliases, revoked credentials, isolation, capability loans, read-only Seal approval and independent support authority");

})().catch(error=>{console.error(error);process.exitCode=1;});
