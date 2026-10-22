// SF-083 frontend: the up-front estimate against the measured SF-054 costs,
// serverless card checkout, purchase status, budget transactions and reads,
// and owner alerts. Offline; no wallet, provider or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loader } = require("./frontend_test_utils.cjs");

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
const { fleet_budget_DeviceAccountBcs } = load("lib/fleet-protocol.ts");

// ── The estimate's inputs are the SF-054 measurements ──
const receipt = quotes.historical_testnet_transactions.find((t) => t.label === "device receipt").effects.gasEffects.gasSummary;
assert.equal(costs.MEASURED.receiptNetMist, BigInt(receipt.computationCost + receipt.storageCost - receipt.storageRebate));
const topUp = quotes.historical_testnet_transactions.find((t) => t.label === "top up device gas").effects.gasEffects.gasSummary;
assert.ok(costs.MEASURED.drawNetMist >= BigInt(topUp.computationCost), "a draw costs at least one computation fee");
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
console.log("PASS: estimate inputs equal the SF-054 measured receipt, Walrus rates, exact RS2 sizes and spot quotes");

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
assert.deepEqual(fns(budget.buildRegisterAndEnrollTx("Pi", wallet, "aarch64", sender, { budgetId, capId, floatMist: BigInt(5) })),
  ["device_registry::register_device", "device_cap::issue_cap", "device_registry::activate_device", "fleet_budget::enroll_device"]);
assert.deepEqual(fns(budget.buildRegisterAndEnrollTx("Pi", wallet, "aarch64", sender)),
  ["device_registry::register_device", "device_cap::issue_cap", "device_registry::activate_device"]);
assert.deepEqual(fns(budget.buildCreateBudgetTx(BigInt(10), budget.DEFAULT_LIMITS, BigInt(2), sender)), ["fleet_budget::new_limits", "fleet_budget::create"]);
assert.deepEqual(fns(budget.buildDepositTx(budgetId, capId, BigInt(1))), ["fleet_budget::deposit"]);
assert.deepEqual(fns(budget.buildWithdrawTx(budgetId, capId, BigInt(1), sender)), ["fleet_budget::withdraw"]);
assert.deepEqual(fns(budget.buildSetPausedTx(budgetId, capId, true)), ["fleet_budget::set_paused"]);
assert.deepEqual(fns(budget.buildFundDeviceTx(budgetId, capId, deviceId, BigInt(1))), ["fleet_budget::fund_device"]);
assert.deepEqual(fns(budget.buildSetDeviceSuspendedTx(budgetId, capId, deviceId, true)), ["fleet_budget::set_device_suspended"]);
assert.throws(() => budget.buildDepositTx(budgetId, capId, BigInt(0)), /positive/);
assert.throws(() => budget.validateLimits({ ...budget.DEFAULT_LIMITS, periodMs: BigInt(0) }), /period/);
assert.throws(() => budget.validateLimits({ ...budget.DEFAULT_LIMITS, floatCap: budget.DEFAULT_LIMITS.periodAllowance + BigInt(1) }), /float cap/);
// Every call the builders make is in the reviewed ABI.
for (const name of ["create", "deposit", "withdraw", "enroll_device", "draw_gas", "set_paused", "fund_device"]) {
  assert.ok(protocol.modules.fleet_budget.functions[name], `fleet_budget::${name} is in the reviewed ABI`);
}
console.log("PASS: registration enrolls atomically with its float; owner budget transactions use the reviewed encoders");

// ── Storage and the swap route ──
assert.match(budget.storageConfig("testnet", undefined).reason, /not configured/);
assert.match(budget.storageConfig("mainnet", "0x5").reason, /pinned Walrus Testnet/);
const storage = budget.storageConfig("testnet", "0x5").config;
assert.equal(budget.swapRoute("testnet").route, "testnet-exchange");
assert.match(budget.swapRoute("mainnet").explanation, /Buy WAL directly/);
const exchange = storage.walrus.exchange.objects[0];
assert.deepEqual(fns(budget.buildSwapAndDepositWalTx(storage, exchange, "0x7", budgetId, capId, BigInt(1000), BigInt(990))),
  ["wal_exchange::exchange_all_for_wal", "fleet_storage::deposit_wal_at_least"]);
assert.throws(() => budget.buildSwapAndDepositWalTx(storage, "0x99", "0x7", budgetId, capId, BigInt(1000), BigInt(990)), /pinned exchange/);
assert.deepEqual(fns(budget.buildBuyStorageTx(storage, "0x7", budgetId, capId, BigInt(1 << 20), 2, BigInt(5), sender)),
  ["system::reserve_space", "fleet_storage::deposit_storage"]);
assert.equal(budget.minWalOut(BigInt(1000), { wal: BigInt(1), sui: BigInt(1) }, 100), BigInt(990));
assert.throws(() => budget.minWalOut(BigInt(1), { wal: BigInt(1), sui: BigInt(0) }, 0), /Invalid/);
console.log("PASS: storage only on the pinned Walrus Testnet build; SUI-only deposit swaps with an on-chain minimum or explains direct WAL");

// ── Reads and alerts ──
const limits = { float_cap: "60", period_ms: "1000", period_allowance: "100", max_draws_per_period: "3", min_draw_interval_ms: "0" };
const raw = (extra) => fleet_budget_DeviceAccountBcs.serialize({ owner_cap_id: capId, limits, suspended: false, period_start_ms: "0",
  drawn_in_period: "0", draws_in_period: "0", last_draw_ms: "0", total_drawn: "60", enrolled_at_ms: "1", ...extra }).toBytes();
async function reads() {
  const accountType = `${manifest.call_package_id}::fleet_budget::DeviceAccount`;
  const field = (type, bytes) => ({ getDynamicField: async () => ({ dynamicField: { value: { type, bcs: bytes } } }) });
  const account = await budget.fetchAccount(field(accountType, raw({ drawn_in_period: "40", draws_in_period: "1" })), budgetId, deviceId);
  assert.equal(account.drawnInPeriod, BigInt(40));
  assert.equal(account.limits.floatCap, BigInt(60));
  await assert.rejects(budget.fetchAccount(field(`0x9::fleet_budget::DeviceAccount`, raw({})), budgetId, deviceId), /Unexpected DeviceAccount type/);
  const view = { id: budgetId, capId, version: BigInt(1), balance: BigInt(1000), paused: false, defaultLimits: budget.DEFAULT_LIMITS,
    lowBalanceThreshold: BigInt(100), deviceCount: BigInt(4), totalDeposited: BigInt(0), totalWithdrawn: BigInt(0), totalDrawn: BigInt(0) };
  const acct = (id, drawn, drawsInPeriod, extra = {}) => ({ deviceId: id, ownerCapId: capId, limits: { floatCap: BigInt(60), periodMs: BigInt(1000),
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
  // A new period clears usage; suspended devices are already handled.
  assert.deepEqual(budget.budgetAlerts(view, [...quiet, acct("0xa4", 100, 3)], BigInt(1000), null), []);
  assert.deepEqual(budget.budgetAlerts(view, [...quiet, acct("0xa4", 100, 3, { suspended: true })], BigInt(5), null), []);
  const empty = budget.budgetAlerts({ ...view, balance: BigInt(0), paused: true }, [], BigInt(5), 1);
  assert.deepEqual(empty.map((a) => a.severity), ["warning", "critical", "critical"]);
  assert.match(budget.budgetAlerts({ ...view, balance: BigInt(50) }, [], BigInt(5), 5)[0].text, /low-balance threshold/);
  assert.match(budget.budgetAlerts(view, [], BigInt(5), 5)[0].text, /5.0 days/);
}
reads().then(() => console.log("PASS: account reads verify their type; at-limit, anomalous, low, empty, paused and runway alerts"),
  (error) => { console.error(error); process.exit(1); });
