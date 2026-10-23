// Request-count fixture with actual query/BCS implementations, 1,000 devices.
const assert = require('node:assert/strict');
const { loader, appRequire, manifestClient } = require('./frontend_test_utils.cjs');
const m = require('../manifests/fixture.localnet.json');
for (const name of require('../interfaces/fleet-v1.json').optional_type_origins.fleet_budget) m.type_origins[name] = m.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), sui = load('lib/sui.ts'), budget = load('lib/fleet-budget.ts'), protocol = load('lib/fleet-protocol.ts');
const health = load('lib/telemetry.ts'), data = load('lib/fleet-data.ts');
const { bcs, TypeTagSerializer } = appRequire('@mysten/sui/bcs');
const { normalizeSuiAddress: id, deriveDynamicFieldID } = appRequire('@mysten/sui/utils');
const { ObjectError } = appRequire('@mysten/sui/client');
const origin = n => `${m.type_origins[n]}::${n}`;
const owner = id('0xa'), budgetId = id('0xb'), capId = id('0xc');
const config = { schema: 1, chainIdentifier: m.chain_identifier, registryId: m.shared_objects.registry.id, packageId: id('0x81'), typeOrigin: id('0x81'), fleetId: id('0xf1'), budgetId, storageId: id('0xf3') };
const devices = Array.from({ length: 1000 }, (_, n) => id(`0x${(10000+n).toString(16)}`));
const objects = new Map(), caps = [];
function field(parent, keyType, Key, name, valueType, Value, value) {
  const key = Key.serialize(name).toBytes(), fieldId = deriveDynamicFieldID(parent, TypeTagSerializer.parseFromStr(keyType), key);
  const content = bcs.struct('Field', { id: bcs.Address, name: Key, value: Value }).serialize({ id: fieldId, name, value }).toBytes();
  objects.set(fieldId, { objectId: fieldId, type: `0x2::dynamic_field::Field<${keyType},${valueType}>`, content });
}
const limits = { float_cap: '60', period_ms: '1000', period_allowance: '100', max_draws_per_period: '3', min_draw_interval_ms: '0' };
for (const [n, device] of devices.entries()) {
  const account = id(`0x${(20000+n).toString(16)}`), cap = id(`0x${(30000+n).toString(16)}`), writer = id(`0x${(40000+n).toString(16)}`);
  caps.push({ objectId: cap, content: protocol.device_registry_DeviceOwnerCapBcs.serialize({ id: cap, device_id: device }).toBytes() });
  objects.set(device, { objectId: device, content: protocol.device_registry_DeviceBcs.serialize({ id: device, name: `Device ${n}`, device_address: device, arch: 'x86_64', meta: { contents: [] }, registered_at: 0, schema_version: 1 }).toBytes() });
  field(budgetId, origin('fleet_budget::DeviceKey'), protocol.fleet_budget_DeviceKeyBcs, {device_id:device}, '0x2::object::ID', bcs.Address, account);
  objects.set(account, { objectId: account, type: origin('fleet_budget::GasAccount'), content: protocol.fleet_budget_GasAccountBcs.serialize({ id: account, budget_id: budgetId, device_id: device, owner_cap_id: capId, reserve: { value: '500' }, limits,
    suspended:false,closed:false,period_start_ms:0,drawn_in_period:0,draws_in_period:0,last_draw_ms:0,total_drawn:0,enrolled_at_ms:0 }).toBytes() });
  field(config.registryId, origin('device_registry::IdentityKey'), bcs.Address, device, origin('device_registry::Identity'), protocol.device_registry_IdentityBcs, { signer: device, revoked: false, owner_cap: cap });
  field(config.fleetId, `${config.typeOrigin}::fleet_data::DeviceKey`, bcs.struct('Key',{device_id:bcs.Address}), {device_id:device}, '0x2::object::ID', bcs.Address, writer);
  objects.set(writer, { objectId: writer, type: `${config.typeOrigin}::fleet_data::DeviceData`, owner: {$kind:'Shared'}, content: data.DeviceDataBcs.serialize({id:writer,version:1,fleet_id:config.fleetId,device_id:device,account_id:account,health_seq:0,health_at_ms:0,message:[],signature:[],batch_seq:0,batch_at_ms:null,emergency_at_ms:null,anchor_count:0}).toBytes() });
}
(async () => {
  let calls = 0;
  const client = manifestClient(m), get = client.getObject;
  client.getObject = async request => {
    calls++;
    if (request.objectId === config.fleetId) return {object:{type:`${config.typeOrigin}::fleet_data::FleetData`,owner:{$kind:'Shared'}, content:data.FleetDataBcs.serialize({id:config.fleetId,version:1,budget_id:budgetId,storage_id:config.storageId,registry_id:config.registryId,chain_identifier:config.chainIdentifier,key_epoch:0,support:[],health_interval_ms:3600000,batch_interval_ms:86400000,emergency_interval_ms:300000,device_count:1000}).toBytes()}};
    return get(request);
  };
  client.getObjects = async ({ objectIds }) => { calls++; assert.ok(objectIds.length <= 50); return { objects: objectIds.map(objectId => objects.get(objectId) ?? new ObjectError(objectId,'missing',{reason:'notFound'})) }; };
  client.listOwnedObjects = async ({ type, cursor }) => { calls++; if (type.endsWith('::Device')) return { objects: [], hasNextPage: false }; const offset = Number(cursor ?? 0); return { objects: caps.slice(offset,offset+50), hasNextPage: offset+50<caps.length, cursor:String(offset+50) }; };
  let before = calls;
  assert.equal((await sui.fetchDevices(client, owner)).length, 1000);
  const inventory = calls-before; // manifest object proofs + ownership pages + batch objects
  before = calls;
  assert.equal((await budget.fetchAccounts(client, budgetId, devices)).length,1000);
  const accounts = calls-before;
  assert.equal(accounts,40,'two sets of 20 object batches; no per-device reads');
  before = calls;
  const result = await data.fetchChainHealth(client, config, devices);
  assert.equal(Object.keys(result).length,1000);
  assert.ok(Object.values(result).every(h => h.status === 'unreported'));
  const chainHealth = calls-before;
  assert.equal(chainHealth,61,'20 identity + 20 index + 20 writer batches + fleet object');
  // Batch mismatch and missing-index behavior remain explicit.
  const bad = {getObjects: async ({objectIds}) => ({objects:objectIds.map(objectId => ({...objects.get(objectId), type:'0x2::object::ID'}))})};
  await assert.rejects(budget.fetchAccounts(bad,budgetId,[devices[0]]), /index type/);
  assert.deepEqual(await budget.fetchAccounts({getObjects:async ({objectIds})=>({objects:objectIds.map(i=>new ObjectError(i,'missing',{reason:'notFound'}))})},budgetId,[devices[0]]),[undefined]);
  const firstAccount = [...objects.values()].find(o=>o.type===origin('fleet_budget::GasAccount'));
  const accountValue = protocol.fleet_budget_GasAccountBcs.parse(firstAccount.content);
  objects.set(firstAccount.objectId,{...firstAccount,content:protocol.fleet_budget_GasAccountBcs.serialize({...accountValue,budget_id:id('0x999')}).toBytes()});
  await assert.rejects(budget.fetchAccounts(client,budgetId,[devices[0]]),/another budget or device/);
  objects.set(firstAccount.objectId,firstAccount);
  const firstWriter = [...objects.values()].find(o=>o.type===`${config.typeOrigin}::fleet_data::DeviceData`);
  const writerValue = data.DeviceDataBcs.parse(firstWriter.content);
  objects.set(firstWriter.objectId,{...firstWriter,content:data.DeviceDataBcs.serialize({...writerValue,device_id:id('0x999')}).toBytes()});
  assert.equal((await data.fetchChainHealth(client,config,[devices[0]]))[devices[0]].status,'unavailable','cross-device writer cannot become health');
  objects.set(firstWriter.objectId,firstWriter);
  const { budgetedFetch } = load('lib/rpc-budget.ts');
  let clock = 0; const starts = [];
  const fetcher = budgetedFetch(async () => { starts.push(clock); return new Response('ok'); }, () => clock, async ms => {clock+=ms;});
  // Includes two health/inventory consumers, budget accounts, proofs, events and receipts.
  const requestCount = 2*(inventory+chainHealth)+accounts+30;
  for (let remaining=requestCount;remaining>0;remaining-=100) await Promise.all(Array.from({length:Math.min(100,remaining)},()=>fetcher('https://fixture.invalid')));
  const peak = Math.max(...starts.map(t=>starts.filter(s=>s>=t&&s<t+30000).length));
  assert.ok(peak<=86,`public-node request budget exceeded: ${peak}`);
  const abort = new AbortController(); abort.abort();
  await assert.rejects(fetcher('https://fixture.invalid',{signal:abort.signal}),/abort/i);
  assert.equal(starts.length,requestCount,'aborted queued requests do not reach transport');
  console.log(JSON.stringify({devices:1000,inventoryRequests:inventory,budgetAccountRequests:accounts,chainHealthRequests:chainHealth,combinedFixtureRequests:requestCount,peakStartsPer30Seconds:peak,virtualDrainMs:clock}));
})().catch(e=>{console.error(e);process.exitCode=1;});
