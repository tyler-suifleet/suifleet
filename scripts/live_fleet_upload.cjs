// SF-083 testnet: the dApp's own fleet-paid upload (dapp/src/lib/fleet-upload.ts)
// with the app's pinned SDKs (@mysten/sui 2.36.0, @mysten/walrus 1.2.35). It
// holds no key: for each transaction it prints {"tx": base64 TransactionData}
// and reads one line {"digest": ...} once scripts/live_fleet_budget.py has
// verified, signed and executed it; the upload then reads the confirmed
// effects from the node itself, as the browser does. Finally it reads the
// blob back from the storage nodes and prints the result.
//   node scripts/live_fleet_upload.cjs RPC_URL SENDER STORAGE_PACKAGE STORAGE BUDGET BUDGET_CAP BLOB_FILE GAS_BUDGET
// NEXT_PUBLIC_DEPLOYMENT_MANIFEST names the fixture's deployment.
const fs = require("node:fs");
const readline = require("node:readline");
const { loader, appRequire } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
const { toBase64 } = appRequire("@mysten/sui/utils");

const [rpc, sender, storagePackage, storageId, budgetId, capId, file, gas] = process.argv.slice(2);
const load = loader();
const budget = load("lib/fleet-budget.ts"), upload = load("lib/fleet-upload.ts");
const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const next = async () => JSON.parse((await lines.next()).value);

(async () => {
  const { config, reason } = budget.storageConfig("testnet", storagePackage);
  if (!config) throw new Error(reason);
  const client = new SuiGrpcClient({ network: "testnet", baseUrl: rpc });
  const execute = async (transaction) => {
    transaction.setSender(sender);
    transaction.setGasBudget(BigInt(gas));
    console.log(JSON.stringify({ tx: toBase64(await transaction.build({ client })) }));
    const { digest } = await next();
    const confirmed = await client.waitForTransaction({ digest, include: { effects: true, events: true } });
    if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) throw new Error(`transaction ${digest} failed`);
    return { digest, events: confirmed.Transaction.events ?? [] };
  };
  const body = fs.readFileSync(file);
  const info = await upload.uploadToFleetStorage(client, { config, storageId, budgetId, capId }, new Uint8Array(body), execute);
  const { walrus, TESTNET_WALRUS_PACKAGE_CONFIG } = appRequire("@mysten/walrus");
  const read = await client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus.readBlob({ blobId: info.blobId });
  console.log(JSON.stringify({ result: { ...info, costFrost: info.costFrost?.toString() }, read_matches: Buffer.from(read).equals(body), read_bytes: read.length }));
  process.exit(0);
})().catch((error) => { console.error(error.stack ?? String(error)); process.exit(1); });
