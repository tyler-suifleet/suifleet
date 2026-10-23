// Read-only SF-129 archive reclaim plan for live runs: the dApp planner over a
// public fullnode and the pinned Walrus SDK's current epoch. No keys or writes.
// Usage: node scripts/reclaim_plan.cjs RPC_URL DATA_CONFIG_JSON STORAGE_ORIGIN DEVICE_ID...
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
(async () => {
  const [rpc, raw, storageOrigin, ...devices] = process.argv.slice(2);
  const config = JSON.parse(raw), client = new SuiGrpcClient({ network: 'testnet', baseUrl: rpc });
  const { walrus, TESTNET_WALRUS_PACKAGE_CONFIG } = await import(appRequire.resolve('@mysten/walrus'));
  const { committee: { epoch } } = await client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus.systemState();
  const plan = await loader()('lib/storage-reclaim.ts').planArchiveReclaim(client, config, storageOrigin, devices, epoch);
  console.log(JSON.stringify({ epoch, scanned: plan.scanned, truncated: plan.truncated, loose: plan.loose,
    anchors: plan.anchors.map((c) => ({ deviceId: c.deviceId, writerId: c.writerId, index: c.index, endEpoch: c.endEpoch, held: c.held, blob: c.anchor.blob_object_id })) }));
})().catch((error) => { console.error(String(error.stack || error).slice(0, 1500)); process.exitCode = 1; });
