// SF-083 frontend: the up-front estimate against the measured SF-054 costs,
// serverless card checkout, purchase status, budget and per-device account
// transactions and reads, owner alerts, fleet storage funding/purchase
// transactions, and the fleet-paid upload flow (pinned-package check,
// register effects, sliver write and certification) against a stubbed
// Walrus client. Offline; no wallet, provider or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loader, appRequire } = require("./frontend_test_utils.cjs");

const root = path.resolve(__dirname, "..");
const evidence = (name) => JSON.parse(fs.readFileSync(path.join(root, "docs/business/sf054", name), "utf8"));
const quotes = evidence("network-quotes.json"), scenario = evidence("scenario.json"), rates = evidence("blob-rate-calculations.json");
const protocol = JSON.parse(fs.readFileSync(path.join(root, "interfaces/fleet-v1.json"), "utf8"));

// A manifest whose package has the fleet budget.
const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifests/fixture.localnet.json"), "utf8"));
for (const name of protocol.optional_type_origins.fleet_budget) manifest.type_origins[name] = manifest.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const load = loader();
const costs = load("lib/network-costs.ts");
const onramp = load("lib/onramp.ts");
const budget = load("lib/fleet-budget.ts");
const { fleet_budget_GasAccountBcs } = load("lib/fleet-protocol.ts");
const { bcs } = appRequire("@mysten/sui/bcs");

// ── The estimate's inputs are the SF-054 measurements ──
const receipt = quotes.historical_testnet_transactions.find((t) => t.label === "device receipt").effects.gasEffects.gasSummary;
assert.equal(costs.MEASURED.receiptNetMist, BigInt(receipt.computationCost + receipt.storageCost - receipt.storageRebate));
const sf083 = JSON.parse(fs.readFileSync(path.join(root, "docs/validation/sf083-testnet.json"), "utf8"));
assert.equal(costs.MEASURED.drawNetMist, BigInt(sf083.runs[1].measured.draw_gas.net_mist), "draw cost measured on testnet");
assert.equal(costs.MEASURED.minComputationMist, BigInt(receipt.computationCost));
assert.equal(costs.MEASURED.walrus.nShards, quotes.walrus.n_shards);
assert.equal(costs.MEASURED.walrus.storagePricePerUnit, BigInt(quotes.walrus.storage_price_per_unit_size));
assert.equal(costs.MEASURED.walrus.writePricePerUnit, BigInt(quotes.walrus.write_price_per_unit_size));
assert.equal(costs.MEASURED.usd.SUI, Number(scenario.token_prices.SUI.usd));
assert.equal(costs.MEASURED.usd.WAL, Number(scenario.token_prices.WAL.usd));
assert.equal(costs.MEASURED.usd.observedAt, scenario.token_prices.SUI.observed_at);
// The on-chain encoded-length formula reproduces the SDK's exact sizes and costs.
for (const rate of rates) {
  assert.equal(costs.encodedBlobLength(rate.ciphertext_bytes), BigInt(rate.encoded_bytes));
  const cost = costs.blobCostFrost(rate.ciphertext_bytes, rate.requested_epochs);
  assert.equal(cost.units, BigInt(rate.units_mib));
  assert.equal(cost.storage, BigInt(rate.storage_frost));
  assert.equal(cost.write, BigInt(rate.write_frost));
}
console.log("PASS: estimate inputs equal the SF-054 measured receipt, Walrus rates, exact RS2 sizes and spot quotes, and the SF-083 testnet draw");

// The SF-054 scenario: 1000 devices, 4 shared releases, one report each per release.
const base = { devices: 1000, months: 1, receiptsPerDeviceMonth: 4, anchorsPerDeviceMonth: 0, releasesPerMonth: 4,
  artifactBytes: rates[0].ciphertext_bytes, dataBytesPerDeviceMonth: 0, storageEpochs: 2, floatMist: BigInt(60_000_000),
  drawMist: BigInt(50_000_000), marginPercent: 0 };
const sf054 = costs.estimate(base);
assert.equal(sf054.artifactFrost.toString(), evidence("scenario-result.json").subtotals["mainnet:estimated:FROST"]);
assert.equal(sf054.deviceTransactions, 4000);
assert.equal(sf054.gasMist, BigInt(4000) * costs.MEASURED.receiptNetMist, "the float covers four receipts: no draws");
assert.equal(sf054.floatsMist, BigInt(1000) * base.floatMist);
assert.equal(sf054.suiDepositMist, sf054.gasMist + sf054.floatsMist);
// Spend beyond the float needs draws, each paying its own gas; headroom applies to spend, not floats.
const busy = costs.estimate({ ...base, devices: 2, receiptsPerDeviceMonth: 200, marginPercent: 10 });
const perDevice = BigInt(200) * costs.MEASURED.receiptNetMist;
const draws = Number((perDevice - base.floatMist + base.drawMist - BigInt(1)) / base.drawMist);
assert.equal(busy.draws, 2 * draws);
const gas = BigInt(400) * costs.MEASURED.receiptNetMist + BigInt(2 * draws) * costs.MEASURED.drawNetMist;
assert.equal(busy.gasMist, gas);
assert.equal(busy.suiDepositMist, gas + gas / BigInt(10) + BigInt(2) * base.floatMist);
const withData = costs.estimate({ ...base, dataBytesPerDeviceMonth: 1 << 20 });
const one = costs.blobCostFrost(1 << 20, 2);
assert.equal(withData.dataFrost, BigInt(1000) * (one.storage + one.write));
assert.throws(() => costs.estimate({ ...base, devices: -1 }), /devices/);
assert.throws(() => costs.estimate({ ...base, storageEpochs: 54 }), /storage epochs/);
assert.throws(() => costs.estimate({ ...base, drawMist: BigInt(0) }), /draw/);
assert.equal(costs.runwayDays(BigInt(100), BigInt(50), 1), 2);
assert.equal(costs.runwayDays(BigInt(100), BigInt(0), 1), null);
assert.equal(costs.formatToken(BigInt(1_500_000_000)), "1.5");
assert.equal(costs.parseToken("1.5"), BigInt(1_500_000_000));
assert.throws(() => costs.parseToken("1.0000000001"), /decimal places/);
assert.throws(() => costs.parseToken("-1"), /positive/);
console.log("PASS: SF-054 scenario reproduced; draws, floats, headroom, data storage and input validation");

// ── Card funding without a server ──
assert.match(onramp.onrampProvider(undefined).reason, /not configured/);
assert.match(onramp.onrampProvider("sk_live_secret").reason, /not a public numeric ID/);
const provider = onramp.onrampProvider("12345").provider;
const wallet = "0x" + "a".repeat(64);
const url = new URL(onramp.checkoutUrl(provider, wallet, "WAL", "12.5"));
assert.equal(url.origin + url.pathname, "https://onramp.money/main/buy/");
assert.deepEqual(Object.fromEntries(url.searchParams), { appId: "12345", walletAddress: wallet, coinCode: "wal", network: "sui", coinAmount: "12.5" });
assert.throws(() => onramp.checkoutUrl(provider, "0x1", "SUI", "1"), /full Sui wallet address/);
assert.throws(() => onramp.checkoutUrl(provider, wallet, "SUI", "0"), /positive amount/);
const pending = { address: wallet, asset: "SUI", amount: "1", startBalance: "100", startedAt: 1_000 };
assert.deepEqual(onramp.purchaseStatus(pending, BigInt(100), 2_000), { state: "waiting" });
assert.deepEqual(onramp.purchaseStatus(pending, BigInt(160), 2_000), { state: "arrived", received: BigInt(60) });
// A spent-down wallet is not a purchase; an unseen one times out as unconfirmed, not as exhaustion.
assert.deepEqual(onramp.purchaseStatus(pending, BigInt(90), 2_000), { state: "waiting" });
assert.deepEqual(onramp.purchaseStatus(pending, BigInt(100), 1_000 + onramp.PURCHASE_TIMEOUT_MS + 1), { state: "unconfirmed" });
assert.match(onramp.FEE_NOTES.service, /no service fee/);
console.log("PASS: hosted checkout URL with public app ID only; purchases count only on arrival and fail separately");

// ── Transactions ──
const sender = "0x" + "1".repeat(64), budgetId = "0x" + "b".repeat(64), capId = "0x" + "c".repeat(64), deviceId = "0x" + "d".repeat(64);
const fns = (tx) => tx.getData().commands.filter((c) => c.$kind === "MoveCall").map((c) => `${c.MoveCall.module}::${c.MoveCall.function}`);
assert.ok(budget.budgetSupported());
const accountId = "0x" + "e".repeat(64);
assert.deepEqual(fns(budget.buildRegisterAndEnrollTx("Pi", wallet, "aarch64", sender, { budgetId, capId, floatMist: BigInt(5), reserveMist: BigInt(10) })),
  ["device_registry::register_device", "device_cap::issue_cap", "device_registry::activate_device", "fleet_budget::enroll_device"]);
assert.deepEqual(fns(budget.buildRegisterAndEnrollTx("Pi", wallet, "aarch64", sender)),
  ["device_registry::register_device", "device_cap::issue_cap", "device_registry::activate_device"]);
assert.deepEqual(fns(budget.buildCreateBudgetTx(BigInt(10), budget.DEFAULT_LIMITS, BigInt(2), sender)), ["fleet_budget::new_limits", "fleet_budget::create"]);
assert.deepEqual(fns(budget.buildDepositTx(budgetId, capId, BigInt(1))), ["fleet_budget::deposit"]);
assert.deepEqual(fns(budget.buildWithdrawTx(budgetId, capId, BigInt(1), sender)), ["fleet_budget::withdraw"]);
assert.deepEqual(fns(budget.buildSetPausedTx(budgetId, capId, true)), ["fleet_budget::set_paused"]);
assert.deepEqual(fns(budget.buildFundDeviceTx(budgetId, capId, accountId, BigInt(1))), ["fleet_budget::fund_device"]);
assert.deepEqual(fns(budget.buildSetDeviceSuspendedTx(budgetId, capId, accountId, true)), ["fleet_budget::set_device_suspended"]);
assert.deepEqual(fns(budget.buildFundReserveTx(budgetId, capId, accountId, BigInt(1))), ["fleet_budget::fund_reserve"]);
assert.deepEqual(fns(budget.buildReclaimReserveTx(budgetId, capId, accountId, BigInt(1))), ["fleet_budget::reclaim_reserve"]);
assert.deepEqual(fns(budget.buildRemoveDeviceTx(budgetId, capId, accountId)), ["fleet_budget::remove_device"]);
assert.deepEqual(fns(budget.buildEnrollDeviceTx(budgetId, capId, deviceId, BigInt(1), BigInt(2))), ["fleet_budget::enroll_device"]);
assert.throws(() => budget.buildFundReserveTx(budgetId, capId, accountId, BigInt(0)), /positive/);
// Owner account changes name the account and only read the budget.
const inputs = (tx) => tx.getData().inputs.map((input) => input.UnresolvedObject?.objectId ?? input.$kind);
assert.ok(inputs(budget.buildSetDeviceSuspendedTx(budgetId, capId, accountId, true)).includes(accountId));
// The reserve: two allowances, at most what the budget holds after the float.
assert.equal(budget.defaultReserve(budget.DEFAULT_LIMITS, BigInt(10_000_000_000), BigInt(60_000_000)), budget.DEFAULT_LIMITS.periodAllowance * BigInt(2));
assert.equal(budget.defaultReserve(budget.DEFAULT_LIMITS, BigInt(100_000_000), BigInt(60_000_000)), BigInt(40_000_000));
assert.equal(budget.defaultReserve(budget.DEFAULT_LIMITS, BigInt(10), BigInt(60_000_000)), BigInt(0));
assert.throws(() => budget.buildDepositTx(budgetId, capId, BigInt(0)), /positive/);
assert.throws(() => budget.validateLimits({ ...budget.DEFAULT_LIMITS, periodMs: BigInt(0) }), /period/);
assert.throws(() => budget.validateLimits({ ...budget.DEFAULT_LIMITS, floatCap: budget.DEFAULT_LIMITS.periodAllowance + BigInt(1) }), /float cap/);
// Every call the builders make is in the reviewed ABI.
for (const name of ["create", "deposit", "withdraw", "enroll_device", "draw_gas", "set_paused", "fund_device", "fund_reserve", "reclaim_reserve", "remove_device"]) {
  assert.ok(protocol.modules.fleet_budget.functions[name], `fleet_budget::${name} is in the reviewed ABI`);
}
// draw_gas reads the fleet budget and writes only the device's own account.
assert.deepEqual(protocol.modules.fleet_budget.functions.draw_gas.parameters.slice(0, 2),
  ["&sui_edge::fleet_budget::FleetBudget", "&mut sui_edge::fleet_budget::GasAccount"]);
console.log("PASS: registration enrolls atomically with its float and reserve; owner budget and account transactions use the reviewed encoders");

// ── Storage and the swap route ──
assert.match(budget.storageConfig("testnet", undefined).reason, /not configured/);
assert.match(budget.storageConfig("mainnet", "0x5").reason, /pinned Walrus Testnet/);
const storage = budget.storageConfig("testnet", "0x5").config;
assert.equal(budget.swapRoute("testnet").route, "testnet-exchange");
assert.match(budget.swapRoute("mainnet").explanation, /Buy WAL directly/);
const exchange = storage.walrus.exchange.objects[0];
assert.deepEqual(fns(budget.buildCreateStorageTx(storage, budgetId, capId, budget.DEFAULT_QUOTA)), ["fleet_storage::new_quota", "fleet_storage::create"]);
assert.throws(() => budget.buildCreateStorageTx(storage, budgetId, capId, { ...budget.DEFAULT_QUOTA, uploadsPerPeriod: BigInt(0) }), /positive/);
assert.deepEqual(fns(budget.buildSwapAndDepositWalTx(storage, exchange, "0x7", budgetId, capId, BigInt(1000), BigInt(990))),
  ["wal_exchange::exchange_all_for_wal", "fleet_storage::deposit_wal_at_least"]);
// SUI only: swap, deposit with its minimum and buy the storage in one transaction.
assert.deepEqual(fns(budget.buildSwapAndDepositWalTx(storage, exchange, "0x7", budgetId, capId, BigInt(1000), BigInt(990), { encodedBytes: BigInt(1 << 20), epochs: 2 })),
  ["wal_exchange::exchange_all_for_wal", "fleet_storage::deposit_wal_at_least", "fleet_storage::buy_storage"]);
assert.throws(() => budget.buildSwapAndDepositWalTx(storage, "0x99", "0x7", budgetId, capId, BigInt(1000), BigInt(990)), /pinned exchange/);
assert.throws(() => budget.buildSwapAndDepositWalTx(storage, exchange, "0x7", budgetId, capId, BigInt(1000), BigInt(0)), /positive minimum/);
assert.deepEqual(fns(budget.buildBuyStorageTx(storage, "0x7", budgetId, capId, BigInt(1 << 20), 2)), ["fleet_storage::buy_storage"]);
assert.throws(() => budget.buildBuyStorageTx(storage, "0x7", budgetId, capId, BigInt(0), 2), /positive size/);
assert.deepEqual(fns(budget.buildExtendPoolTx(storage, "0x7", budgetId, capId, 3)), ["fleet_storage::extend_pool"]);
assert.deepEqual(fns(budget.buildDepositWalTx(storage, "0x7", budgetId, capId, BigInt(5))), ["fleet_storage::deposit_wal"]);
assert.deepEqual(fns(budget.buildWithdrawWalTx(storage, "0x7", budgetId, capId, BigInt(5), sender)), ["fleet_storage::withdraw_wal"]);
assert.equal(budget.storageCost(BigInt(1), 2, BigInt(100)), BigInt(200));
assert.equal(budget.storageCost(BigInt((1 << 20) + 1), 3, BigInt(100)), BigInt(600));
assert.equal(budget.minWalOut(BigInt(1000), { wal: BigInt(1), sui: BigInt(1) }, 100), BigInt(990));
assert.throws(() => budget.minWalOut(BigInt(1), { wal: BigInt(1), sui: BigInt(0) }, 0), /Invalid/);
console.log("PASS: storage only on the pinned Walrus Testnet build; create, fund (SUI swap with an on-chain minimum, or direct WAL), buy, extend and withdraw use fleet_storage");

// ── Reads and alerts ──
const limits = { float_cap: "60", period_ms: "1000", period_allowance: "100", max_draws_per_period: "3", min_draw_interval_ms: "0" };
const raw = (extra) => fleet_budget_GasAccountBcs.serialize({ id: accountId, budget_id: budgetId, device_id: deviceId, owner_cap_id: capId,
  reserve: { value: "500" }, limits, suspended: false, closed: false, period_start_ms: "0", drawn_in_period: "0", draws_in_period: "0",
  last_draw_ms: "0", total_drawn: "60", enrolled_at_ms: "1", ...extra }).toBytes();
async function reads() {
  const accountType = `${manifest.call_package_id}::fleet_budget::GasAccount`;
  // The budget indexes the account's ID; the account is its own shared object.
  const client = (type, bytes, index = "0x2::object::ID") => ({
    getDynamicField: async () => ({ dynamicField: { value: { type: index, bcs: bcs.Address.serialize(accountId).toBytes() } } }),
    getObject: async ({ objectId }) => { assert.equal(objectId, accountId); return { object: { objectId, type, content: bytes } }; },
  });
  const account = await budget.fetchAccount(client(accountType, raw({ drawn_in_period: "40", draws_in_period: "1" })), budgetId, deviceId);
  assert.equal(account.accountId, accountId);
  assert.equal(account.drawnInPeriod, BigInt(40));
  assert.equal(account.reserve, BigInt(500));
  assert.equal(account.limits.floatCap, BigInt(60));
  await assert.rejects(budget.fetchAccount(client(`0x9::fleet_budget::GasAccount`, raw({})), budgetId, deviceId), /Unexpected GasAccount type/);
  await assert.rejects(budget.fetchAccount(client(accountType, raw({}), "0x2::object::UID"), budgetId, deviceId), /device index/);
  await assert.rejects(budget.fetchAccount(client(accountType, raw({ device_id: "0x" + "9".repeat(64) })), budgetId, deviceId), /another budget or device/);
  await assert.rejects(budget.fetchAccount(client(accountType, raw({ budget_id: "0x" + "9".repeat(64) })), budgetId, deviceId), /another budget or device/);
  const view = { id: budgetId, capId, version: BigInt(1), balance: BigInt(1000), paused: false, defaultLimits: budget.DEFAULT_LIMITS,
    lowBalanceThreshold: BigInt(100), deviceCount: BigInt(4), totalDeposited: BigInt(0), totalWithdrawn: BigInt(0), totalPaid: BigInt(0),
    totalAllocated: BigInt(0), totalReclaimed: BigInt(0) };
  const acct = (id, drawn, drawsInPeriod, extra = {}) => ({ accountId, deviceId: id, ownerCapId: capId, reserve: BigInt(500), limits: { floatCap: BigInt(60), periodMs: BigInt(1000),
    periodAllowance: BigInt(100), maxDrawsPerPeriod: BigInt(3), minDrawIntervalMs: BigInt(0) }, suspended: false, periodStartMs: BigInt(0),
    drawnInPeriod: BigInt(drawn), drawsInPeriod: BigInt(drawsInPeriod), lastDrawMs: BigInt(1), totalDrawn: BigInt(drawn), enrolledAtMs: BigInt(0), ...extra });
  const quiet = [acct("0xa1", 10, 1), acct("0xa2", 10, 1), acct("0xa3", 0, 0)];
  assert.deepEqual(budget.budgetAlerts(view, quiet, BigInt(5), null), []);
  const atLimit = budget.budgetAlerts(view, [...quiet, acct("0xa4", 100, 2)], BigInt(5), null);
  assert.deepEqual(atLimit.map((a) => a.deviceId), ["0xa4"]);
  assert.match(atLimit[0].text, /period limit/);
  const anomalous = budget.budgetAlerts(view, [...quiet, acct("0xa5", 60, 2)], BigInt(5), null);
  assert.deepEqual(anomalous.map((a) => a.deviceId), ["0xa5"]);
  assert.match(anomalous[0].text, /far more/);
  // Device reserves running low or empty.
  const reserves = budget.budgetAlerts(view, [...quiet, acct("0xa6", 0, 0, { reserve: BigInt(99) }), acct("0xa7", 0, 0, { reserve: BigInt(0) })], BigInt(5), null);
  assert.deepEqual(reserves.map((a) => [a.deviceId, a.severity]), [["0xa6", "warning"], ["0xa7", "critical"]]);
  assert.match(reserves[1].text, /reserve is empty/);
  // A new period clears usage; suspended devices are already handled.
  assert.deepEqual(budget.budgetAlerts(view, [...quiet, acct("0xa4", 100, 3)], BigInt(1000), null), []);
  assert.deepEqual(budget.budgetAlerts(view, [...quiet, acct("0xa4", 100, 3, { suspended: true })], BigInt(5), null), []);
  const empty = budget.budgetAlerts({ ...view, balance: BigInt(0), paused: true }, [], BigInt(5), 1);
  assert.deepEqual(empty.map((a) => a.severity), ["warning", "critical", "critical"]);
  assert.match(budget.budgetAlerts({ ...view, balance: BigInt(50) }, [], BigInt(5), 5)[0].text, /low-balance threshold/);
  assert.match(budget.budgetAlerts(view, [], BigInt(5), 5)[0].text, /5.0 days/);
  // Storage alerts: no space, no WAL, expiring pool.
  const sv = { id: "0x7", version: BigInt(1), poolSize: BigInt(0), startEpoch: 3, endEpoch: 5, walBalance: BigInt(0), defaultQuota: budget.DEFAULT_QUOTA,
    blobCount: BigInt(0), totalEncodedBytes: BigInt(0), totalWriteFees: BigInt(0) };
  assert.deepEqual(budget.storageAlerts(sv, 4).map((a) => a.severity), ["warning", "warning", "critical"]);
  assert.deepEqual(budget.storageAlerts({ ...sv, poolSize: BigInt(1), walBalance: BigInt(1) }, 2), []);
}

// ── Fleet-paid uploads, against a stubbed Walrus client ──
async function uploads() {
  const walrusSdk = appRequire("@mysten/walrus");
  const target = { config: storage, storageId: "0x" + "7".repeat(64), budgetId, capId };
  const blobObjectId = "0x" + "f".repeat(64);
  const encoded = { blobId: walrusSdk.blobIdFromInt(BigInt(123456789)), rootHash: bcs.u256().serialize(BigInt(42)).toBytes(), metadata: { V1: {} }, sliversByNode: [[], []] };
  const Registered = bcs.struct("FleetUploadRegistered", { storage_id: bcs.Address, blob_object_id: bcs.Address, blob_id: bcs.u256(), size: bcs.u64(),
    encoded_size: bcs.u64(), write_fee: bcs.u64(), end_epoch: bcs.u32(), pool_size: bcs.u64(), wal_balance: bcs.u64() });
  const event = (storageId, blobId) => ({ eventType: `${storage.packageId}::fleet_storage::FleetUploadRegistered`, bcs: Registered.serialize({ storage_id: storageId,
    blob_object_id: blobObjectId, blob_id: blobId, size: "5", encoded_size: "65000000", write_fee: "20000", end_epoch: 9, pool_size: "1", wal_balance: "2" }).toBytes() });
  function stub({ packageId = storage.walrus.walrusPackage, events = [event(target.storageId, BigInt(123456789))] } = {}) {
    const calls = [];
    const walrusClient = {
      systemObject: async () => ({ id: storage.walrus.system, package_id: packageId }),
      systemState: async () => ({ committee: { members: Array.from({ length: 10 }, () => ({})), epoch: 7 } }),
      encodeBlob: async (bytes) => { calls.push(["encode", bytes.length]); return encoded; },
      writeEncodedBlobToNodes: async (options) => { calls.push(["write", options.objectId, options.deletable]); return ["c1", "c2"]; },
      certificateFromConfirmations: async (options) => { calls.push(["certificate", options.blobObjectId]); return { signature: new Uint8Array([1]), signers: [0, 9], serializedMessage: new Uint8Array([2]) }; },
    };
    const client = { $extend: () => ({ walrus: walrusClient }) };
    const execute = async (tx) => { const names = fns(tx); calls.push(["execute", ...names]); return { digest: "d", events: names.includes("fleet_storage::register_fleet_blob") ? events : [] }; };
    return { client, execute, calls };
  }
  const upload = load("lib/fleet-upload.ts");
  const ok = stub();
  const info = await upload.uploadToFleetStorage(ok.client, target, new Uint8Array(5), ok.execute);
  assert.deepEqual(ok.calls, [["encode", 5], ["execute", "fleet_storage::register_fleet_blob"], ["write", blobObjectId, false],
    ["certificate", blobObjectId], ["execute", "fleet_storage::certify_blob"]]);
  assert.deepEqual(info, { blobId: encoded.blobId, blobObjectId, endEpoch: 9, costFrost: BigInt(20000), owner: "fleet", alreadyCertified: false });
  // The live Walrus package must be the pinned one: nothing is registered otherwise.
  const moved = stub({ packageId: "0x" + "5".repeat(64) });
  await assert.rejects(upload.uploadToFleetStorage(moved.client, target, new Uint8Array(5), moved.execute), /not the pinned/);
  assert.deepEqual(moved.calls, []);
  // Without this storage's event for this blob in the confirmed effects, no sliver is written.
  for (const events of [[], [event("0x" + "8".repeat(64), BigInt(123456789))], [event(target.storageId, BigInt(1))]]) {
    const missing = stub({ events });
    await assert.rejects(upload.uploadToFleetStorage(missing.client, target, new Uint8Array(5), missing.execute), /without a fleet upload event/);
    assert.deepEqual(missing.calls.map((c) => c[0]), ["encode", "execute"]);
  }
  // A failed sliver write or certification stops before success is reported.
  const failing = stub();
  const write = failing.client.$extend().walrus;
  failing.client.$extend = () => ({ walrus: { ...write, writeEncodedBlobToNodes: async () => { throw new Error("NotEnoughBlobConfirmationsError"); } } });
  await assert.rejects(upload.uploadToFleetStorage(failing.client, target, new Uint8Array(5), failing.execute), /NotEnoughBlobConfirmations/);
  assert.deepEqual(upload.signersToBitmap([0, 9], 10), new Uint8Array([1, 2]));
  assert.throws(() => upload.signersToBitmap([10], 10), /not in the committee/);
  // walrus.ts uses the fleet store as the only path when one is given.
  const walrusLib = load("lib/walrus.ts");
  const stored = [];
  const result = await walrusLib.uploadArtifact(new File([new Uint8Array(7)], "a.bin"), sender, { kind: "public" }, undefined,
    async (body) => { stored.push(body.length); return info; });
  assert.deepEqual(stored, [7]);
  assert.equal(result.storage.owner, "fleet");
  assert.equal(walrusLib.renewalCommand(info), null);
  assert.deepEqual(walrusLib.storageWarnings(info), []);
}
reads().then(() => console.log("PASS: account reads follow the budget index and verify the account's type, budget and device; reserve, at-limit, anomalous, low, empty, paused, runway and storage alerts"))
  .then(uploads).then(() => console.log("PASS: fleet-paid uploads check the pinned Walrus package, need the registration event, write slivers to the registered blob and certify it; failures stop without success"),
  (error) => { console.error(error); process.exit(1); });
