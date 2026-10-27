const assert = require('node:assert/strict');
const { loader } = require('./frontend_test_utils.cjs');
const load = loader();
const { DAY, periodEpochs, expiryTime, expiryWarning, buildExtendFleetReleaseTx } = load('lib/release-storage.ts');
const schedule = { epoch: 10, firstEpochStartMs: 0, epochDurationMs: DAY, maxEpochs: 53 };
assert.equal(periodEpochs(5, schedule), 5);
assert.equal(periodEpochs(1.1, schedule), 2);
assert.equal(periodEpochs(15, { ...schedule, epochDurationMs: 14 * DAY }), 2);
for (const days of [0, -1, NaN, Infinity, 54]) assert.throws(() => periodEpochs(days, schedule));
for (const maxEpochs of [NaN, 0, Infinity]) assert.throws(() => periodEpochs(1, { ...schedule, maxEpochs }));
assert.equal(expiryTime(15, schedule), 15 * DAY);
assert.match(expiryWarning(10 * DAY, 10 * DAY), /expired/);
assert.match(expiryWarning(17 * DAY, 10 * DAY), /within 7 days/);
assert.equal(expiryWarning(18 * DAY, 10 * DAY), undefined);
assert.match(expiryWarning(undefined), /unknown/);
const target = { config: { packageId: '0xa', walrus: { system: '0xb' } }, storageId: '0xc', budgetId: '0xd', capId: '0xe' };
const tx = buildExtendFleetReleaseTx(target, '0xf', 3).getData();
assert.match(tx.commands[0].MoveCall.function, /extend_blob/);
assert.equal(tx.commands[0].MoveCall.arguments.length, 6);
assert.throws(() => buildExtendFleetReleaseTx(target, '0xf', 0));
(async () => {
  // The actual publisher transport must carry the chosen period, including retries.
  const constants = { NETWORK: 'testnet', WALRUS_PUBLISHER: 'https://publisher.walrus-testnet.walrus.space', WALRUS_EPOCHS: 5 };
  const walrus = loader({ './constants': constants })('lib/walrus.ts');
  const requests = [];
  global.fetch = async url => { requests.push(url); return requests.length === 1 ? new Response('retry', { status: 503 }) : Response.json({ newlyCreated: { blobObject: { id: '0x1', blobId: 'chosen', storage: { endEpoch: 30 } }, cost: '42' } }); };
  const result = await walrus.uploadBlob(new Uint8Array([1, 2, 3]), 'application/octet-stream', '0xa', undefined, 20);
  assert.equal(result.costFrost, 42n);
  assert.equal(result.endEpoch, 30);
  assert.equal(requests.length, 2);
  for (const url of requests) assert.equal(new URL(url).searchParams.get('epochs'), '20');
  await assert.rejects(walrus.uploadBlob(new Uint8Array([1]), 'application/octet-stream', '0xa', undefined, NaN));
  // Pinned SDK cost calculation rounds encoded storage units, not plaintext size.
  const { appRequire } = require('./frontend_test_utils.cjs');
  const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
  const { walrus: extension, TESTNET_WALRUS_PACKAGE_CONFIG } = appRequire('@mysten/walrus');
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: 'http://127.0.0.1:1' });
  const w = client.$extend(extension({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus;
  w.systemState = async () => ({ committee: { n_shards: 1000 }, storage_price_per_unit_size: '100', write_price_per_unit_size: '50' });
  const a = await w.storageCost(100, 1), b = await w.storageCost(100, 5);
  assert.equal(b.storageCost, a.storageCost * 5n);
  assert.equal(b.writeCost, a.writeCost);
  assert.equal(b.totalCost, b.storageCost + b.writeCost);
  const { bcs } = appRequire('@mysten/sui/bcs');
  const Blob = bcs.struct('Blob', { id: bcs.Address, registered_epoch: bcs.u32(), blob_id: bcs.u256(), size: bcs.u64(), encoding_type: bcs.u8(), certified_epoch: bcs.option(bcs.u32()), storage: bcs.struct('Storage', { id: bcs.Address, start_epoch: bcs.u32(), end_epoch: bcs.u32(), storage_size: bcs.u64() }), deletable: bcs.bool() });
  const content = Blob.serialize({ id: '0xf', registered_epoch: 10, blob_id: '42', size: '100', encoding_type: 1, certified_epoch: 10, storage: { id: '0xff', start_epoch: 10, end_epoch: 15, storage_size: '2000000' }, deletable: false }).toBytes();
  client.core.getObjects = async ({ objectIds }) => ({ objects: objectIds.map(objectId => ({ objectId, content, type: objectId.endsWith('f') ? '0xa::blob::Blob' : '0xa::system::System' })) });
  client.core.getMoveFunction = async () => ({ function: { parameters: [null, { body: { $kind: 'datatype', datatype: { typeParameters: [{ $kind: 'datatype', datatype: { typeName: '0xa::wal::WAL' } }] } } }] } });
  w.systemState = async () => ({ committee: { n_shards: 1000, epoch: 10 }, future_accounting: { length: 54 }, storage_price_per_unit_size: '100', write_price_per_unit_size: '50' });
  w.stakingState = async () => ({ epoch_duration: String(DAY), first_epoch_start: '0' });
  w.systemObject = async () => ({ package_id: '0xa' });
  const adapter = { $extend: () => ({ walrus: w }) };
  const walletTx = await load('lib/release-storage.ts').buildExtendWalletReleaseTx(adapter, '0xf', 3);
  walletTx.setSender('0x1');
  await walletTx.toJSON({ supportedIntents: ['CoinWithBalance'] });
  const data = walletTx.getData();
  const call = data.commands.find(c => c.MoveCall?.function === 'extend_blob').MoveCall;
  assert.equal(call.module, 'system');
  const { fromBase64 } = appRequire('@mysten/sui/utils');
  assert.equal(bcs.u32().parse(fromBase64(data.inputs[call.arguments[2].Input].Pure.bytes)), 3);
  assert.equal(data.commands.filter(c => c.MoveCall?.function === 'destroy_zero').length, 1);
  await assert.rejects(load('lib/release-storage.ts').buildExtendWalletReleaseTx(adapter, '0xf', 54));
  const objects = await load('lib/release-storage.ts').releaseObjects({ ...adapter, listOwnedObjects: async () => ({ objects: [{ objectId: '0xf' }], hasNextPage: false }) }, '0x1');
  assert.equal(objects[0].blobId, appRequire('@mysten/walrus').blobIdFromInt(42n));
  assert.equal(objects[0].endEpoch, 15);
  const protocol = load('lib/fleet-protocol.ts');
  for (const sharded of [false, true]) {
    let bindingType;
    const catalog = loader({
      './deployment-manifest': { verifyDeployment: async () => {}, requireManifest: () => ({ type_origins: Object.fromEntries(load('lib/protocol-version.ts').CATALOG_TYPES.map(t => [t, '0xa'])) }), CATALOG_TYPES: load('lib/protocol-version.ts').CATALOG_TYPES, moveType: name => `0xa::${name}` },
      './receipt-shards': { readReceiptDirectory: async () => sharded ? {} : undefined },
    })('lib/catalog.ts');
    const api = {
      getDynamicField: async args => { bindingType = args.name.type; return { dynamicField: { value: { type: '0xa::component_catalog::CatalogBinding', bcs: protocol.component_catalog_CatalogBindingBcs.serialize({ version_id: '0x1' }).toBytes() } } }; },
      getObject: async () => ({ object: { type: '0xa::component_catalog::ComponentVersion', owner: { $kind: 'Immutable' }, objectId: '0x1', content: protocol.component_catalog_ComponentVersionBcs.serialize({ id: '0x1', created_at: '0', schema_version: '1', catalog_id: '0xc', publisher: '0x2', component_name: 'app', version: '1.0.0', platform_os: ['linux'], platform_arch: ['x86_64'], recipe_blob_ids: ['blob'], recipe_sha256s: ['a'.repeat(64)], artifact_sha256s: ['b'.repeat(64)] }).toBytes() } }),
    };
    assert.equal((await catalog.fetchDeploymentVersion(api, '0xd')).manifests[0].recipeBlobId, 'blob');
    assert.equal(bindingType, `0xa::component_catalog::${sharded ? 'ShardedCatalogBindingKey' : 'CatalogBindingKey'}`);
    await assert.rejects(catalog.fetchDeploymentVersion({ ...api, getDynamicField: async () => ({ dynamicField: { value: { type: '0xb::component_catalog::CatalogBinding' } } }) }, '0xd'), /Invalid deployment catalog binding/);
  }
  console.log('release storage: period rounding/limits, expiry/warnings, wallet/fleet extension builders and numeric blob discovery, publisher period/retries and pinned SDK price computation passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
