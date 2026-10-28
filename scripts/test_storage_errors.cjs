// SF-190: real query cache semantics plus rendered storage-choice/usage states.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { QueryClient } = appRequire('@tanstack/react-query');
const { renderToStaticMarkup } = appRequire('react-dom/server');
const { createElement } = appRequire('react');
const format = loader()('lib/storage-errors.ts').formatStorageQueryError;
const raw = new Error('["fleet-storage","testnet","0x1843","0x4253"] data is undefined');
for (const error of [raw, new Error('transport secret'), undefined, null, 'undefined', { message: '0x123' }]) {
  assert.equal(format(error), 'Fleet storage could not be loaded. Try again. If the problem continues, check the selected fleet and network.');
}
let storageResult, storageError, budgetError, readFailure, loading = false, queries = [];
const budget = { id: '0xb', capId: '0xc' }, config = { packageId: '0xp' };
const load = loader({
  '@/hooks/useCurrentAccount': { useCurrentAccount: () => ({ address: '0xa' }) },
  '@mysten/dapp-kit-react': { useCurrentAccount: () => ({ address: '0xa' }), useCurrentClient: () => ({}) },
  '@tanstack/react-query': { useQuery(options) {
    queries.push(options);
    return options.queryKey[0] === 'fleet-budgets' ? { data: [budget], error: budgetError } :
      { data: storageResult, error: storageError, isLoading: loading };
  } },
  './useFleetSelection': { useFleetSelection: () => ({ supported: false }) },
  './useFleetTransaction': { useFleetTransaction: () => ({}) },
  '@/lib/fleet-budget': { storageConfig: () => ({ config }), budgetSupported: () => true,
    fetchStorage: async () => { if (readFailure) throw readFailure; return storageResult; }, fetchBudgets: async () => [budget] },
});
const hook = load('hooks/useFleetStorage.ts').useFleetStorage;
const Choice = load('components/FleetStorageChoice.tsx').FleetStorageChoice;
function choice() {
  const fleet = hook();
  return renderToStaticMarkup(createElement(Choice, { available: !!fleet.target, storage: fleet.storage,
    reason: fleet.reason, checked: true, onChange() {} }));
}
(async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    let html = choice();
    const query = queries.find(q => q.queryKey[0] === 'fleet-storage');
    const empty = await client.fetchQuery(query);
    assert.equal(empty, null, 'absent storage is valid query data');
    assert.equal(client.getQueryState(query.queryKey).status, 'success');
    readFailure = raw;
    await assert.rejects(client.fetchQuery(query), raw, 'read failures remain errors, never absent storage');
    assert.equal(client.getQueryState(query.queryKey).status, 'error');
    readFailure = undefined;
    storageResult = empty;
    html = choice();
    assert.match(html, /Create and fund fleet storage/);
    assert.match(html, /href="\/budget\/">Create or fund fleet storage/);
    assert.match(html, /disabled=""/);
    assert.doesNotMatch(html, /undefined|fleet-storage|0x1843/);
    loading = true;
    assert.match(choice(), /Loading fleet storage/);
    assert.doesNotMatch(choice(), /href=/);
    loading = false; storageError = raw;
    assert.match(choice(), /Fleet storage could not be loaded/);
    assert.doesNotMatch(choice(), /href=/);
    assert.doesNotMatch(choice(), /undefined|fleet-storage|0x1843/);
    storageError = undefined; budgetError = raw;
    assert.match(choice(), /Fleet budget could not be loaded/);
    budgetError = undefined;
    storageResult = { id: '0xs', poolSize: 100n, walBalance: 2n, endEpoch: 600 };
    html = choice();
    assert.match(html, /Prepaid: 100 encoded bytes/);
    assert.doesNotMatch(html, /disabled=|Create or fund/);

    for (const reason of ['Storage is not configured for this network.', 'Loading your fleet budget…']) {
      html = renderToStaticMarkup(createElement(Choice, { available: false, reason, checked: false, onChange() {} }));
      assert.doesNotMatch(html, /href=/);
    }
    // Execute the actual RetentionPanel query function with the real reader and
    // QueryClient: notFound is a normal first-approval state, not a query error.
    const fs = require('node:fs');
    const panel = fs.readFileSync(require('node:path').join(__dirname, '../dapp/src/components/FleetBudget.tsx'), 'utf8');
    const queryFn = panel.match(/const deviceView = useQuery\(\{[\s\S]*?queryFn: (.*?) \}\);/)[1];
    const { ObjectError } = appRequire('@mysten/sui/client');
    const retention = loader()('lib/retention.ts');
    let retentionFailure = new ObjectError('notFound', 'No archive policy', { objectId: '0xs', reason: 'notFound' });
    const retentionClient = {
      movePackageService: { getDatatype: async () => ({ response: { datatype: { definingId: '0x1' } } }) },
      getDynamicField: async () => { throw retentionFailure; },
    };
    const deviceQuery = { queryKey: ['device-retention', 'testnet', '0xs', '0xp'],
      queryFn: Function('fetchDeviceRetention', 'client', 'config', 'storage', 'return ' + queryFn)(retention.fetchDeviceRetention, retentionClient, config, '0xs') };
    assert.equal(await client.fetchQuery(deviceQuery), null);
    assert.equal(client.getQueryState(deviceQuery.queryKey).status, 'success', 'first archive approval remains enabled');
    retentionFailure = raw;
    await assert.rejects(client.fetchQuery(deviceQuery), raw);
    assert.equal(client.getQueryState(deviceQuery.queryKey).status, 'error');

    const usageLoad = loader({
      '@mysten/dapp-kit-react': { useCurrentClient: () => ({}) },
      '@tanstack/react-query': { useQuery: () => ({ error: raw }) },
      '@/hooks/useFleetStorage': { useFleetStorage: () => ({}) },
      './FleetBudget': { useWalrusState: () => ({}) },
      '@/hooks/useFleetDataConfig': { useFleetDataConfig: () => undefined },
      '@/hooks/useFleetSelection': { useFleetSelection: () => ({}) },
    });
    html = renderToStaticMarkup(createElement(usageLoad('components/DataUsage.tsx').DataUsage));
    assert.match(html, /Storage usage could not be loaded/);
    assert.doesNotMatch(html, /undefined|fleet-storage|0x1843/);
    console.log('PASS: storage empty/loading/funded/error states, real QueryClient null result, absent archive-policy success/read failure, conditional setup links, safe formatter and rendered usage errors');
  } finally { client.clear(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

for (const file of ['components/DataUsage.tsx', 'components/FleetBudget.tsx', 'components/FleetSettings.tsx', 'hooks/useFleetStorage.ts', 'components/StorageReclaim.tsx', 'app/components/page.tsx']) {
  const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '../dapp/src', file), 'utf8');
  assert.doesNotMatch(source, /\.error\??\.message/, file + ' must not render raw query errors');
  assert.match(source, /formatStorageQueryError/);
}

const settingsSource = require('node:fs').readFileSync(require('node:path').join(__dirname, '../dapp/src/components/FleetSettings.tsx'), 'utf8');
assert.doesNotMatch(settingsSource, /\$\{(?:walrusError|gasError)\}/, 'price read errors must use the shared formatter');
