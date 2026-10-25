// SF-131 frontend: the fleet-settings estimator. Mainnet-rescaled measured gas
// against the recorded read-only quote, the single price table, each setting's
// network cost per device and per fleet kept apart from the SuiFleet price,
// the deposit recommendation that follows the settings, and live quotes.
// Offline; no wallet, provider or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { loader } = require("./frontend_test_utils.cjs");

const root = path.resolve(__dirname, "..");
const json = (file) => JSON.parse(fs.readFileSync(path.join(root, file), "utf8"));
const snapshot = json("docs/business/sf131/mainnet-quote.json");
const protocol = json("interfaces/fleet-v1.json");
const manifest = json("manifests/fixture.localnet.json");
const withoutSettings = structuredClone(manifest);
for (const name of [...protocol.optional_type_origins.fleet_budget, ...protocol.optional_type_origins.fleet_settings]) manifest.type_origins[name] = manifest.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const load = loader();
const costs = load("lib/network-costs.ts");
const pricing = load("lib/fleet-pricing.ts");
const settings = load("lib/fleet-settings-cost.ts");
const quotes = load("lib/cost-quote.ts");
const saving = load("lib/fleet-settings-tx.ts");
const { MEASURED, MAINNET_QUOTE, netMist, rescaled } = costs;
const near = (actual, expected, tolerance, what) =>
  assert.ok(Math.abs(actual - expected) <= expected * tolerance, `${what}: ${actual} is not within ${tolerance * 100}% of ${expected}`);

// ── The Mainnet quote is the recorded read-only snapshot; only computation rescales ──
assert.equal(snapshot.read_only, true);
assert.equal(snapshot.transactions_submitted, 0);
assert.equal(snapshot.chain.mainnet.chain_identifier, "4btiuiMPvEENsttpZC7CZ53DruC3MAgfznDbASZ7DR6S");
assert.equal(snapshot.chain.testnet.chain_identifier, "69WiPg3DAQiwdxfncX6wYQ2siKwAe6L9BZthQea3JNMD");
assert.equal(MAINNET_QUOTE.referenceGasPrice, BigInt(snapshot.chain.mainnet.reference_gas_price));
assert.equal(MEASURED.referenceGasPrice, BigInt(snapshot.chain.testnet.reference_gas_price), "measurements were paid at Testnet's price");
assert.equal(snapshot.chain.mainnet.storage_gas_price, snapshot.chain.testnet.storage_gas_price, "storage gas is the same protocol constant");
assert.equal(snapshot.chain.mainnet.storage_rebate_rate, snapshot.chain.testnet.storage_rebate_rate);
assert.equal(MAINNET_QUOTE.walrusStoragePrice, BigInt(snapshot.walrus_mainnet.storage_price_per_unit_size));
assert.equal(MAINNET_QUOTE.walrusWritePrice, BigInt(snapshot.walrus_mainnet.write_price_per_unit_size));
assert.equal(MAINNET_QUOTE.gasObservedAt, Date.parse(snapshot.observed_at_utc));
assert.equal(MAINNET_QUOTE.walrusObservedAt, Date.parse(snapshot.observed_at_utc));
assert.equal(MAINNET_QUOTE.usd.SUI, snapshot.token_market_quotes.SUI.usd);
assert.equal(MAINNET_QUOTE.usd.WAL, snapshot.token_market_quotes.WAL.usd);
assert.equal(MAINNET_QUOTE.usdObservedAt, snapshot.token_market_quotes.WAL.last_updated_at * 1000);

const receipt = MEASURED.receipt;
assert.equal(netMist(receipt, MEASURED.referenceGasPrice), receipt.computation + receipt.storage - receipt.rebate, "identity at the measured price");
assert.equal(costs.MAINNET.receiptNetMist, receipt.computation / BigInt(10) + receipt.storage - receipt.rebate);
assert.equal(costs.MAINNET.receiptNetMist, BigInt(5_252_232));
assert.equal(netMist(MEASURED.draw, BigInt(2000)) - netMist(MEASURED.draw, BigInt(1000)), MEASURED.draw.computation, "only computation scales");
assert.equal(costs.MAINNET.archiveNetMist, MEASURED.fleetData.archive.reduce((sum, e) => sum + e.computation / BigInt(10) + e.storage - e.rebate, BigInt(0)));
assert.deepEqual(rescaled(MAINNET_QUOTE), costs.MAINNET);
// The estimate follows the quote: Testnet's price costs more gas; Walrus prices come from the quote.
const base = { devices: 10, months: 2, receiptsPerDeviceMonth: 4, anchorsPerDeviceMonth: 0, releasesPerMonth: 1, artifactBytes: 1 << 20,
  dataBytesPerDeviceMonth: 0, storageEpochs: 3, floatMist: BigInt(100_000_000_000), drawMist: BigInt(50_000_000), marginPercent: 0 };
const mainnet = costs.estimate(base), testnet = costs.estimate(base, { ...MAINNET_QUOTE, referenceGasPrice: BigInt(1000) });
assert.equal(mainnet.quote, MAINNET_QUOTE);
assert.equal(mainnet.gasMist, BigInt(80) * costs.MAINNET.receiptNetMist);
assert.ok(testnet.gasMist > mainnet.gasMist);
const doubled = costs.estimate(base, { ...MAINNET_QUOTE, walrusStoragePrice: MAINNET_QUOTE.walrusStoragePrice * BigInt(2), walrusWritePrice: BigInt(0) });
assert.ok(doubled.artifactFrost > mainnet.artifactFrost);
assert.throws(() => costs.estimate(base, { ...MAINNET_QUOTE, referenceGasPrice: BigInt(0) }), /positive/);
console.log("PASS: Mainnet quote equals the read-only snapshot; Testnet gas rescales by computation only; estimates use the quote's gas and Walrus prices");

// ── One configurable price table (docs/BUSINESS_PLAN.md 2026-10-23 proposal) ──
const table = pricing.DEFAULT_PRICE_TABLE;
assert.equal(pricing.validatePriceTable(table), table);
const priceOf = (key, value) => pricing.optionFor(table, key, value).usdPerDeviceMonth;
const MIN = 60_000, HOUR = 60 * MIN, DAY = 24 * HOUR;
assert.deepEqual(table.base.tiers, [{ upToDevices: 10_000, usdPerDeviceMonth: 0.08 }, { upToDevices: 100_000, usdPerDeviceMonth: 0.05 }, { upToDevices: 1_000_000, usdPerDeviceMonth: 0.04 }]);
assert.deepEqual([table.base.customFromUsdPerDeviceMonth, table.base.monthlyMinimumUsd, table.base.activationUsdPerDevice, table.base.annualPrepayDiscountPercent], [0.03, 49, 0.10, 10]);
assert.deepEqual([priceOf("checkIntervalMs", 5 * MIN), priceOf("checkIntervalMs", 15 * MIN), priceOf("checkIntervalMs", 30 * MIN), priceOf("checkIntervalMs", HOUR)], [0.05, 0.01, 0, 0]);
assert.deepEqual([priceOf("healthIntervalMs", HOUR), priceOf("healthIntervalMs", 6 * HOUR), priceOf("healthIntervalMs", DAY)], [0.12, 0.03, 0]);
assert.deepEqual([priceOf("archiveIntervalMs", DAY), priceOf("archiveIntervalMs", 7 * DAY)], [0.08, 0]);
assert.deepEqual([priceOf("archiveRetentionDays", 42), priceOf("archiveRetentionDays", 182), priceOf("archiveRetentionDays", 365)], [0, 0.02, 0.04]);
assert.equal(table.addOns.encryptedDelivery.usdPerDeviceMonth, 0.02);
// Defaults: 30-minute checks, 60-second rollout checks, daily health, weekly archives kept six weeks (SF-130).
assert.deepEqual(settings.defaultValues(table), { checkIntervalMs: 30 * MIN, rolloutCheckIntervalMs: MIN, healthIntervalMs: DAY, archiveIntervalMs: 7 * DAY, archiveRetentionDays: 42 });
// A deployment can replace the table; a malformed one is refused whole.
const clone = () => JSON.parse(JSON.stringify(table));
assert.deepEqual(pricing.loadPriceTable(undefined), { table });
const cheaper = clone(); cheaper.base.tiers[0].usdPerDeviceMonth = 0.045;
assert.equal(pricing.loadPriceTable(JSON.stringify(cheaper)).table.base.tiers[0].usdPerDeviceMonth, 0.045);
assert.match(pricing.loadPriceTable("{").error, /JSON|Unexpected|Expected/);
const broken = [
  [(t) => { t.settings.healthIntervalMs[0].default = true; }, /exactly one default/],
  [(t) => { t.settings.checkIntervalMs[0].usdPerDeviceMonth = -1; }, /price/],
  [(t) => { t.base.tiers[1].upToDevices = 10; }, /rise/],
  [(t) => { t.currency = "EUR"; }, /USD/],
  [(t) => { t.settings.archiveIntervalMs[1].value = DAY; }, /distinct/],
  [(t) => { delete t.settings.archiveRetentionDays; }, /at least one option/],
  [(t) => { t.base.annualPrepayDiscountPercent = 150; }, /at most 100/],
];
for (const [mutate, error] of broken) { const t = clone(); mutate(t); assert.match(pricing.loadPriceTable(JSON.stringify(t)).error, error); }
console.log("PASS: price table holds the business-plan defaults, tiers and add-on prices; overrides are validated and refused whole");

// ── The SuiFleet price: tiers, minimum, add-ons, annual discount, activation ──
assert.deepEqual(pricing.basePrice(table, 1000), { usd: 80, custom: false });
near(pricing.basePrice(table, 150_000).usd, 10_000 * 0.08 + 90_000 * 0.05 + 50_000 * 0.04, 1e-9, "third tier");
const huge = pricing.basePrice(table, 1_200_000);
near(huge.usd, 800 + 4500 + 900_000 * 0.04 + 200_000 * 0.03, 1e-9, "custom tier"); assert.equal(huge.custom, true);
const defaults = settings.defaultValues(table);
const small = pricing.fleetPrice(table, 100, { values: defaults, logUploads: false, encryptedDelivery: false, annualPrepay: false });
assert.equal(small.minimumApplied, true); assert.equal(small.monthly, 49); near(small.activationOneTime, 10, 1e-9, "activation");
// The plan's heavy profile at 10,000 devices: 15-minute checks, hourly health, daily archives = $0.29 a device, $2,900.
const heavy = { ...defaults, checkIntervalMs: 15 * MIN, healthIntervalMs: HOUR, archiveIntervalMs: DAY };
const heavyPrice = pricing.fleetPrice(table, 10_000, { values: heavy, logUploads: true, encryptedDelivery: false, annualPrepay: false });
near(heavyPrice.monthly, 2900, 1e-9, "heavy fleet"); near(heavyPrice.perDevice, 0.29, 1e-9, "heavy device");
const annual = pricing.fleetPrice(table, 10_000, { values: heavy, logUploads: true, encryptedDelivery: true, annualPrepay: true });
near(annual.monthly, (2900 + 200) * 0.9, 1e-9, "Seal add-on and annual discount");
// With log uploads off (the new-plan default) the archive cadence and retention are not charged.
const unlogged = pricing.fleetPrice(table, 10_000, { values: { ...heavy, archiveRetentionDays: 365 }, logUploads: false, encryptedDelivery: false, annualPrepay: false });
near(unlogged.monthly, 2900 - 800, 1e-9, "no daily-archive add-on without log uploads");
assert.deepEqual(unlogged.addOns.filter((a) => pricing.LOG_SETTING_KEYS.includes(a.key)).map((a) => a.usdPerFleet), [0, 0]);
assert.throws(() => pricing.fleetPrice(table, 10, { values: { ...defaults, healthIntervalMs: 2 * HOUR }, logUploads: true, encryptedDelivery: false, annualPrepay: false }), /not an option/);
console.log("PASS: SuiFleet price: graduated tiers, custom above 1M, $49 minimum, add-ons per device and fleet, Seal, annual discount, activation");

// ── Settings: SF-130 bounds and per-network retention epochs ──
const onMainnet = settings.toSettings(defaults, "mainnet");
assert.deepEqual(onMainnet, { checkIntervalMs: 30 * MIN, rolloutCheckIntervalMs: MIN, healthIntervalMs: DAY, archiveIntervalMs: 7 * DAY, archiveRetentionEpochs: 3 });
assert.equal(settings.toSettings(defaults, "testnet").archiveRetentionEpochs, 42);
assert.equal(settings.toSettings({ ...defaults, archiveRetentionDays: 365 }, "mainnet").archiveRetentionEpochs, 27);
assert.throws(() => settings.toSettings({ ...defaults, archiveRetentionDays: 365 }, "testnet"), /archiveRetentionEpochs/);
assert.equal(settings.validateSettings({ ...onMainnet, rolloutCheckIntervalMs: 0 }).rolloutCheckIntervalMs, 0, "rollout checks may be off");
for (const [field, value] of [["checkIntervalMs", MIN], ["checkIntervalMs", 7 * HOUR], ["rolloutCheckIntervalMs", 10_000], ["healthIntervalMs", 2 * DAY],
  ["archiveIntervalMs", 8 * DAY], ["archiveRetentionEpochs", 0], ["archiveRetentionEpochs", 54], ["healthIntervalMs", 1.5 * HOUR + 0.5]]) {
  assert.throws(() => settings.validateSettings({ ...onMainnet, [field]: value }), new RegExp(field));
}
// Every price-table option is within the bounds on Mainnet.
for (const key of pricing.SETTING_KEYS) for (const o of table.settings[key]) settings.toSettings({ ...defaults, [key]: o.value }, "mainnet");
console.log("PASS: settings follow SF-130 bounds; retention days round up to whole Walrus epochs per network; every offered option is valid on Mainnet");

// ── Network cost per setting, per device and per fleet, at the Mainnet quote ──
// New plans default to log uploads off (BUSINESS_PLAN.md, 2026-10-23): archives cost nothing and the
// deposit funds none, whatever cadence is selected, until uploads are explicitly enabled.
assert.equal(settings.DEFAULT_WORKLOAD.logUploads, false);
const unloggedWorkload = { ...settings.DEFAULT_WORKLOAD, devices: 1000 };
assert.equal(settings.uploadedLogBytes(unloggedWorkload), 0);
for (const key of ["archiveIntervalMs", "archiveRetentionDays"]) {
  const off = settings.settingNetworkCost(key, settings.toSettings({ ...defaults, archiveIntervalMs: DAY }, "mainnet"), unloggedWorkload, MAINNET_QUOTE);
  assert.deepEqual([off.mistPerDevice, off.frostPerDevice, off.detail], [0, 0, "log uploads off"], key);
}
{
  const off = settings.depositInput(settings.toSettings(defaults, "mainnet"), unloggedWorkload, 3);
  assert.deepEqual([off.dataUploadsPerDeviceMonth, off.dataBytesPerDeviceMonth, off.serverlessData], [0, 0, false], "no archives funded by default");
  const offDeposit = settings.recommendDeposit(settings.toSettings(defaults, "mainnet"), unloggedWorkload, 3, MAINNET_QUOTE);
  const onDeposit = settings.recommendDeposit(settings.toSettings(defaults, "mainnet"), { ...unloggedWorkload, logUploads: true }, 3, MAINNET_QUOTE);
  assert.ok(onDeposit.suiDepositMist > offDeposit.suiDepositMist && onDeposit.walDepositFrost > offDeposit.walDepositFrost, "enabling uploads funds archives");
}
// Explicitly enabled: the business plan's typical 10 MiB of logs per device a month.
const workload = { ...unloggedWorkload, logUploads: true };
const line = (key, values) => settings.settingNetworkCost(key, settings.toSettings(values, "mainnet"), workload, MAINNET_QUOTE);
const usdPerMist = MAINNET_QUOTE.usd.SUI / 1e9;
assert.equal(line("checkIntervalMs", { ...defaults, checkIntervalMs: 5 * MIN }).usdPerDevice, 0, "checks are reads: no network fee");
assert.equal(line("rolloutCheckIntervalMs", defaults).usdPerDevice, 0);
const daily = line("healthIntervalMs", defaults), hourly = line("healthIntervalMs", { ...defaults, healthIntervalMs: HOUR });
near(daily.mistPerDevice, settings.MONTH_MS / DAY * Number(costs.MAINNET.recurringHealthNetMist), 1e-12, "daily health");
near(hourly.usdPerDevice, 24 * daily.usdPerDevice, 1e-9, "hourly is 24 times daily");
near(daily.usdPerFleet, daily.usdPerDevice * 1000, 1e-9, "per fleet");
// Cross-check against the plan's rounded estimates (within 10%): health $0.003 daily, $0.072 hourly; a v3 receipt about $0.00514 (SF-127 localnet effects rescaled to Mainnet).
near(daily.usdPerDevice, 0.003, 0.10, "daily health vs plan"); near(hourly.usdPerDevice, 0.072, 0.10, "hourly health vs plan");
near(Number(costs.MAINNET.receiptNetMist) * usdPerMist, 0.00514, 0.01, "v3 receipt estimate");
// Archives: daily costs more than weekly; longer retention costs more Walrus storage; no logs, no cost.
const weekly = line("archiveIntervalMs", defaults), dailyArchives = line("archiveIntervalMs", { ...defaults, archiveIntervalMs: DAY });
assert.ok(dailyArchives.usdPerDevice > weekly.usdPerDevice);
assert.ok(line("archiveRetentionDays", { ...defaults, archiveRetentionDays: 182 }).frostPerDevice > line("archiveRetentionDays", defaults).frostPerDevice);
// Before deposit reclaim (SF-129) a weekly archive's Walrus storage deposit dominates: the plan's "about $0.10".
near(weekly.usdPerDevice, 0.10, 0.10, "weekly archives before reclaim");
assert.equal(settings.settingNetworkCost("archiveIntervalMs", onMainnet, { ...workload, logBytesPerDeviceMonth: 0 }, MAINNET_QUOTE).usdPerDevice, 0);
// The fleet total is network cost only: it never includes the SuiFleet price.
const fleet = settings.fleetNetworkCost(onMainnet, workload, MAINNET_QUOTE);
const sum = fleet.lines.reduce((s, l) => s + l.usdPerDevice, 0) + fleet.base.receipts.usdPerDevice;
near(fleet.usdPerDevice, sum, 1e-12, "lines add up");
near(fleet.usdPerFleet, sum * 1000 + fleet.base.releasesUsdPerFleet, 1e-12, "fleet adds releases once");
// Until deposit reclaim (SF-129), archives remain the largest line, while v3
// receipts now carry their own storage cost rather than an immediate cleanup rebate.
near(fleet.base.receipts.usdPerDevice, workload.deploymentsPerMonth * Number(costs.MAINNET.receiptNetMist) * usdPerMist, 1e-12, "base includes v3 receipt storage");
const archiveUsd = fleet.lines.find((l) => l.key === "archiveIntervalMs").usdPerDevice;
assert.ok(archiveUsd > fleet.base.receipts.usdPerDevice);
assert.ok(fleet.lines.every((l) => l.usdPerDevice <= archiveUsd));
assert.ok(fleet.usdPerDevice > pricing.basePrice(table, 1).usd);
console.log("PASS: each setting's network cost per device and per fleet, at Mainnet-rescaled gas and Walrus rates, matching the plan's estimates; kept apart from price");

// ── The deposit follows the chosen settings, fleet size and months ──
const deposit = (values, w = workload, months = 3) => settings.recommendDeposit(settings.toSettings(values, "mainnet"), w, months, MAINNET_QUOTE);
const input = settings.depositInput(onMainnet, workload, 3);
assert.equal(input.healthSummariesPerDeviceMonth, 31, "daily health rounds up to whole reports");
assert.equal(input.dataUploadsPerDeviceMonth, 5, "weekly archives round up");
assert.equal(input.storageEpochs, 3);
const standard = deposit(defaults);
assert.ok(deposit({ ...defaults, healthIntervalMs: HOUR }).suiDepositMist > standard.suiDepositMist, "hourly health needs more SUI");
assert.ok(deposit({ ...defaults, archiveRetentionDays: 365 }).walDepositFrost > standard.walDepositFrost, "longer retention needs more WAL");
assert.ok(deposit(defaults, { ...workload, devices: 2000 }).suiDepositMist > standard.suiDepositMist, "more devices need more");
assert.ok(deposit(defaults, workload, 12).suiDepositMist > standard.suiDepositMist, "more months need more");
assert.deepEqual(settings.depositShortfall(standard, undefined, undefined), { suiMist: standard.suiDepositMist, walFrost: standard.walDepositFrost, enough: false });
assert.equal(settings.depositShortfall(standard, standard.suiDepositMist, standard.walDepositFrost).enough, true);
const shortSui = settings.depositShortfall(standard, standard.suiDepositMist - BigInt(1), standard.walDepositFrost);
assert.deepEqual([shortSui.suiMist, shortSui.walFrost, shortSui.enough], [BigInt(1), BigInt(0), false]);
assert.equal(settings.depositShortfall({ suiDepositMist: standard.suiDepositMist, walDepositFrost: BigInt(0) }, standard.suiDepositMist, undefined).enough, true);
console.log("PASS: the recommended deposit follows health, archives, retention, fleet size and months; shortfall against the budget and storage balances");

// ── Saving: SF-130's fleet_settings::set, only with its type origins and a budget ──
{
  const budget = { id: "0x" + "b1".repeat(32), capId: "0x" + "c1".repeat(32) };
  assert.match(saving.settingsSaver(budget, withoutSettings).reason, /no on-chain fleet settings/);
  assert.equal(saving.settingsSaver(budget, withoutSettings).build, undefined);
  assert.match(saving.settingsSaver(undefined).reason, /create one on the Network budget page/);
  const chosen = settings.toSettings({ ...defaults, healthIntervalMs: 3_600_000 }, "testnet");
  const data = saving.settingsSaver(budget).build(chosen).getData();
  const [call] = data.commands.map((c) => c.MoveCall);
  assert.equal(data.commands.length, 1);
  assert.equal(`${call.module}::${call.function}`, "fleet_settings::set");
  assert.deepEqual(call.arguments.slice(0, 2).map((a) => data.inputs[a.Input].UnresolvedObject.objectId), [budget.id, budget.capId]);
  assert.throws(() => saving.settingsSaver(budget).build({ ...chosen, checkIntervalMs: 1 }), /Invalid fleet settings/);
}
console.log("PASS: saving builds one SF-130 fleet_settings::set on the owner's budget and cap; refused without the settings type origins, a budget or valid values");

// ── Live quotes: each part keeps its own source and date ──
const at = Date.parse("2026-10-24T08:00:00Z");
const live = quotes.combineQuote("testnet", { gas: { referenceGasPrice: BigInt(1000), observedAt: at } });
assert.equal(live.referenceGasPrice, BigInt(1000)); assert.match(live.gasSource, /testnet .*live/); assert.equal(live.gasObservedAt, at);
assert.equal(live.walrusStoragePrice, MAINNET_QUOTE.walrusStoragePrice); assert.match(live.walrusSource, /recorded/);
const walrus = quotes.combineQuote("mainnet", { walrus: { storagePrice: BigInt(60_000), writePrice: BigInt(110_000), epoch: 43, observedAt: at } });
assert.match(walrus.walrusSource, /epoch 43 \(live\)/); assert.equal(walrus.walrusWritePrice, BigInt(110_000)); assert.match(walrus.gasSource, /recorded/);
assert.throws(() => quotes.combineQuote("mainnet", { gas: { referenceGasPrice: BigInt(0), observedAt: at } }), /positive/);
assert.equal(quotes.quoteDate(MAINNET_QUOTE.gasObservedAt), "2026-10-23 21:22 UTC");
(async () => {
  const reply = (status, body) => async (url, init) => {
    assert.equal(url, quotes.USD_QUOTE_URL); assert.equal(init.credentials, "omit");
    return { ok: status === 200, status, json: async () => body };
  };
  const usd = await quotes.fetchUsdQuote(reply(200, { sui: { usd: 1.1, last_updated_at: 1792800000 }, "walrus-2": { usd: 0.04, last_updated_at: 1792800100 } }));
  assert.deepEqual(usd, { SUI: 1.1, WAL: 0.04, observedAt: 1792800000 * 1000 });
  const priced = quotes.combineQuote("mainnet", { usd });
  assert.equal(priced.usdSource, "CoinGecko spot (live)");
  assert.ok(costs.estimate(base, priced).usd.total > costs.estimate(base).usd.total);
  await assert.rejects(quotes.fetchUsdQuote(reply(429, {})), /HTTP 429/);
  await assert.rejects(quotes.fetchUsdQuote(reply(200, { sui: { usd: 1 } })), /malformed/);
  await assert.rejects(quotes.fetchUsdQuote(reply(200, { sui: { usd: -1, last_updated_at: 1 }, "walrus-2": { usd: 1, last_updated_at: 1 } })), /malformed/);
  console.log("PASS: live gas, Walrus and USD quotes replace only their own parts, each dated; failed or malformed quotes are refused");
})().catch((error) => { console.error(error); process.exit(1); });
