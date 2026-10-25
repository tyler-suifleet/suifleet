const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { bcs } = appRequire('@mysten/sui/bcs');
const { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
const load = loader();
const { DEFAULT_PRICE_TABLE: table, validatePriceTable, fleetPrice } = load('lib/fleet-pricing.ts');
const { dataPrice } = load('lib/data-pricing.ts');
const costs = load('lib/fleet-settings-cost.ts');
const { MAINNET_QUOTE } = load('lib/network-costs.ts');
const values = costs.defaultValues(table), settings = costs.toSettings(values, 'mainnet');
assert.equal(dataPrice(table, 100, 10_000_000, 42).monthlyUsd, 0);
assert.equal(dataPrice(table, 0, 10_000_000, 42).monthlyUsd, 0);
const priced = dataPrice(table, 100, 20_000_000, 84);
assert.equal(priced.writeUsd, 0.1);
assert(priced.storageUsd > 0);
assert.equal(priced.allowance, 1_000_000_000);
const custom = structuredClone(table); custom.data.writeUsdPerGB = 0.2;
assert.equal(dataPrice(validatePriceTable(custom), 100, 20_000_000, 84).writeUsd, 0.2);
for (const bad of [undefined, {}, { ...table.data, writeUsdPerGB: -1 }, { ...table.data, includedBytesPerDeviceMonth: 1.1 }]) {
  assert.throws(() => validatePriceTable({ ...table, data: bad }));
}
for (const args of [[-1, 0, 42], [1, NaN, 42], [1, Infinity, 42], [1, 1, -1], [1e10, 1e10, 42]]) assert.throws(() => dataPrice(table, ...args));
const selection = { values, logUploads: false, encryptedDelivery: false, annualPrepay: true };
const base = fleetPrice(table, 100, selection);
assert.equal(fleetPrice(table, 100, { ...selection, dataBytesPerDeviceMonth: 20_000_000, retentionDays: 84 }).monthly, base.monthly + priced.monthlyUsd);
const workload = { ...costs.DEFAULT_WORKLOAD, logUploads: false, dataBytesPerDeviceMonth: 20_000_000 };
assert.equal(costs.depositInput(settings, workload, 3).dataBytesPerDeviceMonth, 20_000_000);
assert(costs.recommendDeposit(settings, workload, 3, MAINNET_QUOTE).walDepositFrost > costs.recommendDeposit(settings, { ...workload, dataBytesPerDeviceMonth: 0 }, 3, MAINNET_QUOTE).walDepositFrost);
assert.equal(costs.uploadedLogBytes({ ...workload, logUploads: true }), 20_000_000 + workload.logBytesPerDeviceMonth);
console.log('PASS data allowance, decimal GB, retention, configured rates, invalid inputs, minimum/discount separation and prepaid volume');

// Render the real usage panel while switching selected fleets. Configuration
// and query identity must follow selection rather than the deployment default.
{
 const React = appRequire('react');
 const { renderToStaticMarkup } = appRequire('react-dom/server');
 let selected, identity = 'account-a:fleet-a', query;
 const budget = { id: id('0x10') }, storage = { id: id('0x20') };
 const { DataUsage } = loader({
   '@mysten/dapp-kit-react': { useCurrentClient: () => ({}) },
   '@tanstack/react-query': { useQuery: options => { query = options; return {}; } },
   '@/hooks/useFleetStorage': { useFleetStorage: () => ({ budget, storage, config: { packageId: id('0x30') } }) },
   './FleetBudget': { useWalrusState: () => ({ data: { epoch: 1 } }) },
   '@/hooks/useFleetDataConfig': { useFleetDataConfig: () => selected },
   '@/hooks/useFleetSelection': { useFleetSelection: () => ({ identity }) },
 })('components/DataUsage.tsx');
 selected = { budgetId: budget.id, storageId: storage.id };
 renderToStaticMarkup(React.createElement(DataUsage));
 assert.equal(query.enabled, true);
 const firstKey = query.queryKey;
 selected = { budgetId: id('0x11'), storageId: id('0x21') };
 assert.match(renderToStaticMarkup(React.createElement(DataUsage)), /does not match/);
 assert.equal(query.enabled, false);
 budget.id = selected.budgetId; storage.id = selected.storageId;
 identity = 'account-b:fleet-b';
 renderToStaticMarkup(React.createElement(DataUsage));
 assert.equal(query.enabled, true);
 assert.notDeepEqual(query.queryKey, firstKey);
 assert(query.queryKey.includes(identity));
 console.log('PASS selected-fleet usage panel, transitional binding refusal and account/fleet query isolation');
}

(async () => {
 const { readDataMeter } = load('lib/data-metering.ts');
 const { FleetStorageBcs } = load('lib/fleet-budget.ts');
 const { RecordBcs } = load('lib/retention.ts');
 const origin = id('0x123'), storage = id('0x456'), budget = id('0x789'), device = id('0xabc');
 const type = name => `${origin}::fleet_storage::${name}`;
 const quota = { period_ms: '1000', encoded_bytes_per_period: '10000', uploads_per_period: '10', max_blob_size: '1000', write_fees_per_period: '1000' };
 const Usage = bcs.struct('Usage', { quota: bcs.struct('Quota', Object.fromEntries(Object.keys(quota).map(key => [key, bcs.u64()]))), period_start_ms: bcs.u64(), encoded_bytes_in_period: bcs.u64(), uploads_in_period: bcs.u64(), write_fees_in_period: bcs.u64(), total_encoded_bytes: bcs.u64(), total_uploads: bcs.u64() });
 const content = FleetStorageBcs.serialize({ id: storage, version: '3', budget_id: budget, pool: null, wal: {value:'0'}, default_quota:quota, blob_count:'2', total_encoded_bytes:'1000', total_write_fees:'0' }).toBytes();
 const record = (device_id, size, end_epoch) => RecordBcs.serialize({ device_id, blob_id: '1', size: String(size), encoded_size:'200', end_epoch, uploaded_at_ms:'1000' }).toBytes();
 const field = (key, valueType, keyId, bytes) => ({ $kind:'DynamicField', name:{type:type(key),bcs:bcs.Address.serialize(keyId).toBytes()}, valueType:type(valueType), value:{bcs:bytes} });
 const fields = [field('RecordKey','BlobRecord',id('0x1'),record(device,10,5)), field('RecordKey','BlobRecord',id('0x2'),record(null,20,1)), field('UsageKey','Usage',device,Usage.serialize({quota,period_start_ms:'0',encoded_bytes_in_period:'0',uploads_in_period:'0',write_fees_in_period:'0',total_encoded_bytes:'700',total_uploads:'2'}).toBytes())];
 let mode='', reads=0;
 const client = {
   getObject:async () => ({object:{objectId:storage,type:type('FleetStorage'),owner:{$kind:'Shared'},content, version: mode==='race' ? String(reads++) : '1', digest:'d'}}),
   listDynamicFields:async ({cursor}) => mode === 'cycle' ? {dynamicFields:[],hasNextPage:true,cursor:'same'} : {dynamicFields:cursor ? fields.slice(1) : fields.slice(0,1), hasNextPage:!cursor,cursor:cursor ? null : 'second'},
 };
 const window={fromMs:0,toMs:4000,epoch:2,firstEpochStartMs:0,epochDurationMs:1000};
 const result = await readDataMeter(client,storage,budget,origin,window);
 assert.equal(result.writtenEncodedBytes,1000n); assert.equal(result.retainedBytes,30n); assert.equal(result.storedBytes,10n); assert.equal(result.storedByteMs,30000n);
 assert.equal(result.streams.find(s=>s.device===device).writtenEncodedBytes,700n);
 assert.equal(result.streams.find(s=>s.device===null).writtenEncodedBytes,300n); // includes released owner writes
 await assert.rejects(readDataMeter(client,storage,id('0x1'),origin,window),/binding/);
 mode='cycle'; await assert.rejects(readDataMeter(client,storage,budget,origin,window),/page/);
 mode='race'; await assert.rejects(readDataMeter(client,storage,budget,origin,window),/changed/);
 mode=''; fields[1].valueType=type('Usage'); await assert.rejects(readDataMeter(client,storage,budget,origin,window),/type/);
 console.log('PASS paginated original-origin records, fleet/device/owner counters, expiry, byte-time, released writes, binding/type/cursor/race failures');
})().catch(error=>{console.error(error);process.exitCode=1;});
