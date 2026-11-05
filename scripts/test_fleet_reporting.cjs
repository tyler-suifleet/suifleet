const assert = require('node:assert/strict'), fs = require('node:fs');
const {loader, appRequire, manifestClient} = require('./frontend_test_utils.cjs');
const {bcs} = appRequire('@mysten/sui/bcs');
const {normalizeSuiAddress:id,toBase58} = appRequire('@mysten/sui/utils');
const {ObjectError} = appRequire('@mysten/sui/client');
const manifest = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
const catalog = JSON.parse(fs.readFileSync('interfaces/fleet-v1.json'));
for(const name of [...catalog.optional_type_origins.fleet_budget,...catalog.optional_type_origins.customer]) manifest.type_origins[name]=manifest.call_package_id;
manifest.network='testnet'; manifest.chain_identifier='4c78adac';
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(manifest);
process.env.NEXT_PUBLIC_FLEET_STORAGE_PACKAGE=id('0x999');
const release={packageId:id('0x81'),upgradeCapId:id('0x82'),storagePackageId:id('0x51'),storageUpgradeCapId:id('0x52')};
process.env.NEXT_PUBLIC_FLEET_REPORTING_RELEASE=JSON.stringify(release);
const load=loader(), reporting=load('lib/fleet-reporting.ts'), data=load('lib/fleet-data.ts'), budget=load('lib/fleet-budget.ts'), protocol=load('lib/fleet-protocol.ts'), {UpgradeCapBcs}=load('lib/move-bcs.ts');
const fleet=n=>({id:id(`0xf${n}`),customerId:id(`0xc${n}`),budgetId:id(`0xb${n}`),devices:[id(`0xd${n}`)],administrator:true});
const configs=[1,2].map(n=>({schema:1,chainIdentifier:manifest.chain_identifier,registryId:manifest.shared_objects.registry.id,
 packageId:release.packageId,typeOrigin:release.packageId,fleetId:id(`0xa${n}`),budgetId:fleet(n).budgetId,storageId:id(`0x9${n}`),compactHistoryOrigin:release.packageId,streamRetentionOrigin:release.packageId}));
function fixture(options={}) {
 const base=manifestClient(manifest), objects=new Map();
 const shared=(type,content,objectId)=>({object:{type,content,objectId,owner:{$kind:'Shared',Shared:{initialSharedVersion:'1'}}}});
 function pkg(packageId,module,names,capId) {
  objects.set(packageId,{object:{objectBcs:bcs.Object.serialize({data:{Package:{id:packageId,version:'1',moduleMap:new Map(),linkageTable:new Map(),typeOriginTable:names.map(datatypeName=>({moduleName:module,datatypeName,package:packageId}))}},owner:{Immutable:true},previousTransaction:toBase58(new Uint8Array(32)),storageRebate:0}).toBytes()}});
  objects.set(capId,{object:{type:'0x2::package::UpgradeCap',content:UpgradeCapBcs.serialize({id:capId,package:options.staleRelease?id('0x999'):packageId,version:'1',policy:0}).toBytes()}});
 }
 pkg(release.packageId,'fleet_data',['FleetData','DeviceData','DeviceKey','CompactHeadKey','StreamRetentionKey','LocatorKey'],release.upgradeCapId);
 pkg(release.storagePackageId,'fleet_storage',['FleetStorage','RetentionKey'],release.storageUpgradeCapId);
 const limits={float_cap:'1',period_ms:'86400000',period_allowance:'1',max_draws_per_period:'1',min_draw_interval_ms:'0'};
 for(const [i,c] of configs.entries()) {
  const n=i+1;
  const fields={id:c.budgetId,version:'2',registry_id:manifest.shared_objects.registry.id,owner_cap_id:id(`0xe${n}`),balance:{value:'0'},paused:false,default_limits:limits,low_balance_threshold:'0',device_count:'1',total_deposited:'0',total_withdrawn:'0',total_paid:'0',total_allocated:'0',total_reclaimed:'0'};
  objects.set(c.budgetId,shared(`${manifest.type_origins['fleet_budget::FleetBudget']}::fleet_budget::FleetBudget`,protocol.fleet_budget_FleetBudgetBcs.serialize(fields).toBytes(),c.budgetId));
  objects.set(c.fleetId,shared(`${release.packageId}::fleet_data::FleetData`,data.FleetDataBcs.serialize({id:c.fleetId,version:'1',budget_id:options.crossData?configs[1-i].budgetId:c.budgetId,storage_id:c.storageId,registry_id:c.registryId,chain_identifier:c.chainIdentifier,key_epoch:'0',support:[],health_interval_ms:'86400000',batch_interval_ms:'86400000',emergency_interval_ms:'300000',device_count:'0'}).toBytes(),c.fleetId));
  const accountId=id(`0x70${n}`),writerId=id(`0x60${n}`);
  objects.set(accountId,shared(`${manifest.type_origins['fleet_budget::GasAccount']}::fleet_budget::GasAccount`,protocol.fleet_budget_GasAccountBcs.serialize({id:accountId,budget_id:c.budgetId,device_id:fleet(n).devices[0],owner_cap_id:id(`0xe${n}`),reserve:{value:'0'},limits,suspended:false,closed:false,period_start_ms:'0',drawn_in_period:'0',draws_in_period:'0',last_draw_ms:'0',total_drawn:'0',enrolled_at_ms:'0'}).toBytes(),accountId));
  objects.set(writerId,shared(`${release.packageId}::fleet_data::DeviceData`,data.DeviceDataBcs.serialize({id:writerId,version:'1',fleet_id:c.fleetId,device_id:fleet(n).devices[0],account_id:accountId,health_seq:'0',health_at_ms:'0',message:[],signature:[],batch_seq:'0',batch_at_ms:null,emergency_at_ms:null,anchor_count:'0'}).toBytes(),writerId));
  objects.set(c.storageId,shared(`${options.wrongOrigin?id('0xdead'):release.storagePackageId}::fleet_storage::FleetStorage`,budget.FleetStorageBcs.serialize({id:c.storageId,version:'3',budget_id:options.crossStorage?configs[1-i].budgetId:c.budgetId,pool:null,wal:{value:'0'},default_quota:{period_ms:'86400000',encoded_bytes_per_period:'1000',uploads_per_period:'2',max_blob_size:'1000',write_fees_per_period:'1000'},blob_count:'0',total_encoded_bytes:'0',total_write_fees:'0'}).toBytes(),c.storageId));
 }
 return {...base, async getObject(request) {return objects.get(request.objectId)??base.getObject(request);},async getDynamicField({parentId,name}) {
  if(options.rpcError)throw Error('transport unavailable');
  if(name.type.endsWith('::fleet_data::DeviceKey')) {
   if(options.missingWriter)throw new ObjectError(parentId,'missing',{reason:'notFound'});
   const n=configs.findIndex(c=>c.fleetId===parentId)+1;
   return {dynamicField:{value:{type:'0x2::object::ID',bcs:bcs.Address.serialize(id(`0x60${n}`)).toBytes()}}};
  }
  if(name.type.endsWith('::fleet_budget::DeviceKey')) {
   const n=configs.findIndex(c=>c.budgetId===parentId)+1;
   return {dynamicField:{value:{type:'0x2::object::ID',bcs:bcs.Address.serialize(id(`0x70${n}`)).toBytes()}}};
  }
  if(options.absent)throw new ObjectError(parentId,'missing',{reason:'notFound'});
  assert.equal(name.type,`${release.packageId}::fleet_data::LocatorKey`);
  const c=configs.find(c=>c.budgetId===parentId);assert(c);
  return {dynamicField:{value:{type:"0x2::object::ID",bcs:reporting.ReportingLocatorBcs.serialize(options.spoof?configs[1].fleetId:c.fleetId).toBytes()}}};
 }};
}
(async()=>{
 const selectedStorage=await reporting.resolveFleetStorageRelease(fixture(),fleet(1));
 assert.equal(selectedStorage.config.packageId,release.storagePackageId);assert.equal(selectedStorage.config.storageOrigin,release.storagePackageId);assert.equal(selectedStorage.storageId,configs[0].storageId);assert.equal(selectedStorage.config.retentionOrigin,release.storagePackageId);
 assert.equal(load('lib/retention.ts').retentionOrigin(selectedStorage.config),release.storagePackageId);
 await assert.rejects(()=>reporting.resolveFleetStorageRelease(fixture({rpcError:true}),fleet(1)));
 const legacyStorage=await reporting.resolveFleetStorageRelease(fixture({absent:true}),fleet(1));assert.equal(legacyStorage.config.packageId,id('0x999'));
 for(let n=1;n<=2;n++)assert.deepEqual(await reporting.resolveFleetReporting(fixture(),fleet(n)),configs[n-1]);
 for(const options of [{crossData:true},{crossStorage:true},{wrongOrigin:true},{staleRelease:true},{spoof:true},{rpcError:true}])await assert.rejects(()=>reporting.resolveFleetReporting(fixture(options),fleet(1)));
 assert.equal(await reporting.resolveFleetReporting(fixture({absent:true}),fleet(1)),undefined);
 const tx=await reporting.buildProvisionReporting(fixture({absent:true}),fleet(1));
 assert.deepEqual(tx.getData().commands.map(c=>c.MoveCall.function),['new_quota','provision']);
 const wrapped=load('lib/customer-fleets.ts').withManagedBudgetCap(tx,fleet(1),id('0xe1'));
 assert.deepEqual(wrapped.getData().commands.map(c=>c.MoveCall.function),['borrow_budget_cap','new_quota','provision','return_budget_cap']);
 await assert.rejects(()=>reporting.buildProvisionReporting(fixture({absent:true}),{...fleet(1),administrator:false}),/administrator/);
 await assert.rejects(()=>reporting.buildProvisionReporting(fixture(),fleet(1)),/already set up/);
 await assert.rejects(()=>reporting.reportingClientSettings(fixture(),fleet(1),fleet(2).devices[0]),/outside/);
 const settings=await reporting.reportingClientSettings(fixture(),fleet(1),fleet(1).devices[0]);
 assert.match(settings,/\[budget\]/);assert.match(settings,/\[fleet_data\]/);assert(settings.includes(`fleet_id = "${configs[0].fleetId}"`));
 assert(settings.includes(`writer_id = "${id('0x601')}"`));
 assert(settings.includes(`[fleet_data.managed]\ncustomer_id = "${fleet(1).customerId}"\nfleet_id = "${fleet(1).id}"`));
 assert(!settings.includes('keypair_path'));assert(!settings.includes('[runtime]'));
 await assert.rejects(()=>reporting.reportingClientSettings(fixture({missingWriter:true}),fleet(1),fleet(1).devices[0]),/Enroll device/);
 console.log('PASS: two dynamic fleets; spoofed locator, cross-budget data/storage, wrong origin, stale release, RPC failure and read-only authority rejected; atomic capability-loan builder and retry recovery');
})().catch(e=>{console.error(e);process.exit(1)});
