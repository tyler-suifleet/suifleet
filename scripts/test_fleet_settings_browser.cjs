// SF-131 Fleet settings page in a real browser: every option shows its network
// cost and SuiFleet price per device and per fleet, the quotes show their
// source and date, the deposit follows the chosen settings, and saving without
// enough deposit warns before anything is signed. The wallet, chain reads and
// budget are controlled (scripts/browser/fleet-settings-fakes.js); pricing,
// estimates, the deployment manifest gate and the SF-130 fleet_settings::set
// transaction the page builds are real, and the wallet fake decodes it.
//   CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/test_fleet_settings_browser.cjs
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");

const src = path.resolve(__dirname, "../dapp/src"), fakes = path.resolve(__dirname, "browser/fleet-settings-fakes.js");
const importers = new Set([path.join(src, "components/StreamRetention.tsx"), path.join(src, "components/FleetSettings.tsx"), path.join(src, "components/FleetBudget.tsx"), path.join(src, "hooks/useCostQuote.ts")]);
const faked = new Set(["@mysten/dapp-kit-react", "@mysten/walrus", "@/hooks/useDevices", "@/hooks/useFleetStorage", "@/hooks/useFleetTransaction"]);
const fakeDependencies = { name: "fleet-settings-fakes", setup(b) {
  b.onResolve({ filter: /.*/ }, (args) => importers.has(args.importer) && faked.has(args.path) ? { path: fakes } : undefined);
} };
// A Testnet deployment with SF-130 settings, and one without them.
const root = path.resolve(__dirname, "..");
const protocol = JSON.parse(require("node:fs").readFileSync(path.join(root, "interfaces/fleet-v1.json"), "utf8"));
const withoutSettings = { ...JSON.parse(require("node:fs").readFileSync(path.join(root, "manifests/fixture.localnet.json"), "utf8")), network: "testnet", chain_identifier: "4c78adac" };
const withSettings = structuredClone(withoutSettings);
for (const name of protocol.optional_type_origins.fleet_settings) withSettings.type_origins[name] = withSettings.call_package_id;
const PACKAGE = "0x" + withSettings.call_package_id.slice(2).padStart(64, "0");
const BUDGET = "0x" + "b1".repeat(32), CAP = "0x" + "c1".repeat(32), CLOCK = "0x" + "0".repeat(63) + "6";
const BUDGET2 = "0x" + "b2".repeat(32), CAP2 = "0x" + "c2".repeat(32);
const setCall = (values, budget = BUDGET, cap = CAP) => ({ target: `${PACKAGE}::fleet_settings::set`, objects: [budget, cap, CLOCK], values });
const USD = { sui: { usd: 2.5, last_updated_at: 1792900000 }, "walrus-2": { usd: 0.05, last_updated_at: 1792900060 } };

(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-fleet-settings-"));
  let server, browser;
  try {
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/fleet-settings.jsx")], bundle: true, platform: "browser", outfile: bundle, jsx: "automatic",
      banner: { js: "var process = { env: { NEXT_PUBLIC_DEPLOYMENT_MANIFEST: JSON.stringify(window.__SF_MANIFEST_OBJECT) } };" }, nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": src },
      define: { "process.env.NODE_ENV": '"development"' }, plugins: [fakeDependencies], logLevel: "warning" });
    server = http.createServer(async (req, res) => {
      const js = req.url === "/app.js";
      const manifest = new URL(req.url, "http://x").searchParams.get("scenario") === "unsupported" ? withoutSettings : withSettings;
      res.setHeader("Content-Type", js ? "text/javascript" : "text/html");
      res.end(js ? await fs.readFile(bundle) : `<!doctype html><html><head><title>Fleet settings</title></head><body><div id="root"></div>
        <script>window.__SF_MANIFEST_OBJECT = ${JSON.stringify(manifest)};</script><script src="/app.js"></script></body></html>`);
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const origin = `http://127.0.0.1:${server.address().port}`;

    async function open(scenario) {
      const page = await browser.newPage(), errors = [], usdRequests = [];
      page.on("pageerror", (error) => errors.push(error.message));
      // No real third-party call: CoinGecko is answered locally, and only when the owner asks.
      await page.route("https://api.coingecko.com/**", (route) => { usdRequests.push(route.request().url());
        return route.fulfill({ status: 200, contentType: "application/json", headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify(USD) }); });
      await page.goto(`${origin}/?scenario=${scenario}`);
      const body = () => page.textContent("body");
      const waitText = (value) => page.waitForFunction((v) => document.body.textContent.includes(v), value, { timeout: 30_000 })
        .catch(async (error) => { throw new Error(`${error.message}; waiting for ${value}; page shows: ${await body()}`); });
      const control = () => page.evaluate(() => window.__control);
      const text = (id) => page.getByTestId(id).textContent();
      const cells = async (id) => (await page.getByTestId(id).locator("td").allTextContents()).map((t) => t.trim());
      const depositSui = async () => Number(/for \d+ month\(s\) of these settings([\d.]+) SUI/.exec(await text("deposit"))[1]);
      await waitText("Network cost per month, at cost");
      await waitText("testnet reference gas price (live)");
      return { page, errors, usdRequests, waitText, control, text, cells, depositSui };
    }

    // ── Not enough deposit: prices, quotes, deposit, and the warning before signing ──
    {
      const { page, errors, usdRequests, waitText, control, text, cells, depositSui } = await open("short");
      // The fleet size starts from the enrolled devices; the owner sets it to 1,000.
      assert.equal(await page.getByLabel("Devices", { exact: true }).first().inputValue(), "3");
      await page.getByLabel("Devices", { exact: true }).first().fill("1000");
      await waitText("Per fleet $80.00");
      // Defaults from the price table are selected.
      for (const id of ["checkIntervalMs-1800000", "rolloutCheckIntervalMs-60000", "healthIntervalMs-86400000", "archiveIntervalMs-604800000", "archiveRetentionDays-42"]) {
        assert.ok(await page.locator(`#${id}`).isChecked(), `${id} is the default`);
      }
      // Each option: usage, network cost per device and fleet, SuiFleet price per device and fleet.
      const hourly = await cells("healthIntervalMs-3600000");
      assert.match(hourly[1], /731 signed health reports/);
      assert.match(hourly[2], /^\$0\.\d{4}$/); assert.notEqual(hourly[2], "$0.0000");
      assert.deepEqual(hourly.slice(4), ["$0.1200", "$120.00"]);
      assert.deepEqual((await cells("healthIntervalMs-86400000")).slice(4), ["included", "included"]);
      const fiveMinutes = await cells("checkIntervalMs-300000");
      assert.match(fiveMinutes[1], /8,766 reads, no network fee/);
      assert.deepEqual(fiveMinutes.slice(2), ["$0", "$0", "$0.0500", "$50.00"]);
      // Log uploads are off for new plans: archives cost nothing, are not charged and are not funded.
      assert.equal(await page.getByLabel(/Estimate with device log uploads/).isChecked(), false);
      assert.equal(await page.getByLabel("Log data per device per month (MiB)").count(), 0);
      for (const id of ["archiveIntervalMs-86400000", "archiveIntervalMs-604800000", "archiveRetentionDays-42"]) {
        const row = await cells(id);
        assert.deepEqual(row.slice(1, 4), ["log uploads off", "$0", "$0"], `${id} without log uploads`);
      }
      assert.deepEqual((await cells("archiveIntervalMs-86400000")).slice(4), ["not charged", "not charged"]);
      const unlogged = await depositSui();
      // SF142: expected non-log device data changes both the proposed price and
      // the actual prepaid recommendation, even with log uploads disabled.
      const dataInput = page.getByLabel("Additional device data per device per month (MiB)");
      await dataInput.fill("20");
      await page.waitForFunction(() => !document.querySelector('[data-testid="archiveIntervalMs-604800000"]').textContent.includes("log uploads off"));
      assert.ok(await depositSui() > unlogged, "additional device data funds archives without enabling logs");
      assert.match(await text("suifleet-price"), /10,000,000,000 bytes written per month included/);
      assert.match(await text("suifleet-price"), /\$0\.1000\/GB written and \$0\.0500\/GB-month/);
      assert.doesNotMatch(await text("suifleet-price"), /Device data: \$0 per month/);
      await dataInput.fill("0");
      await page.waitForFunction(() => document.querySelector('[data-testid="archiveIntervalMs-604800000"]').textContent.includes("log uploads off"));
      assert.equal(await depositSui(), unlogged);

      await page.getByLabel(/Estimate with device log uploads/).check();
      await page.getByLabel("Log data per device per month (MiB)").waitFor();
      assert.equal(await page.getByLabel("Log data per device per month (MiB)").inputValue(), "10");
      assert.match((await cells("archiveIntervalMs-604800000"))[1], /^4\.3 archives/);
      assert.deepEqual((await cells("archiveIntervalMs-86400000")).slice(4), ["$0.0800", "$80.00"]);
      assert.ok(await depositSui() > unlogged, "enabling log uploads funds the archives");
      await page.getByLabel(/Estimate with device log uploads/).uncheck();
      await page.waitForFunction(() => document.querySelector('[data-testid="archiveIntervalMs-604800000"]').textContent.includes("log uploads off"));
      assert.equal(await depositSui(), unlogged);
      // Testnet's one-day epochs cannot keep archives a year within SF-130's 53-epoch bound.
      assert.ok(await page.locator("#archiveRetentionDays-365").isDisabled());
      assert.match((await cells("archiveRetentionDays-365"))[1], /Unavailable here: needs 365 Walrus epochs of 1 day\(s\); the limit is 53/);
      // Network cost and the SuiFleet price are shown apart.
      assert.match(await text("network-cost"), /paid from your fleet budget/);
      assert.doesNotMatch(await text("network-cost"), /\$80\.00/);
      assert.match(await text("suifleet-price"), /Base \$80\.00; add-ons \$0/);
      assert.match(await text("suifleet-price"), /Activation \$100\.00 one-time/);
      // Quotes: the live Testnet gas and Walrus reads, each dated; USD stays recorded until fetched.
      const quote = await text("quote");
      assert.match(quote, /Gas: 1000 MIST per unit, testnet reference gas price \(live\), \d{4}-\d\d-\d\d \d\d:\d\d UTC/);
      assert.match(quote, /Walrus: 11000 FROST per MiB-epoch and 20000 per MiB written, testnet Walrus epoch 7 \(live\)/);
      assert.match(quote, /CoinGecko spot \(recorded\), 2026-10-23 21:21 UTC/);
      assert.deepEqual(usdRequests, []);
      const before = await text("deposit");
      await page.getByText("Technical cost assumptions and exchange rates", { exact: true }).click();
      await page.getByRole("button", { name: "Fetch live USD prices from CoinGecko" }).click();
      await waitText("CoinGecko spot (live)");
      assert.match(await text("quote"), /SUI \$2\.5, WAL \$0\.05, CoinGecko spot \(live\), 2026-10-2\d \d\d:\d\d UTC/);
      assert.equal(usdRequests.length, 1);
      assert.notEqual(await text("deposit"), before, "the indicative value follows the live USD quote");
      // The deposit follows the settings.
      const daily = await depositSui();
      await page.locator("#healthIntervalMs-3600000").check();
      await waitText("add-ons $120.00");
      const hourlyDeposit = await depositSui();
      assert.ok(hourlyDeposit > daily * 2, `hourly health needs a much larger deposit: ${daily} → ${hourlyDeposit}`);
      assert.match(await text("deposit"), /Your fleet budget holds 0\.01 SUI; fleet storage holds 0 WAL/);
      assert.match(await text("deposit"), /Short by [\d.]+ SUI and [\d.]+ WAL/);
      // Saving warns first and signs nothing.
      await page.getByRole("button", { name: "Save settings" }).click();
      await waitText("Not enough deposit for these settings");
      assert.match(await page.getByRole("alert").first().textContent(), /You have not approved any changes yet/);
      assert.equal((await control()).signed.length, 0);
      assert.match(await text("saved"), /No settings saved yet/);
      assert.deepEqual((await control()).settingsReads.slice(0, 1), [BUDGET], "the saved record is read from the fleet budget");
      // Changing a choice withdraws the warning; it must be checked again.
      await page.locator("#archiveIntervalMs-86400000").check();
      await page.waitForFunction(() => !document.body.textContent.includes("Not enough deposit for these settings"));
      await page.getByRole("button", { name: "Save settings" }).click();
      await waitText("Not enough deposit for these settings");
      // Saving anyway signs exactly the chosen settings, once.
      await page.getByRole("button", { name: "Save anyway" }).click();
      await waitText("Settings saved in FAKEDIGEST1");
      const { signed } = await control();
      assert.deepEqual(signed, [{ sender: "0x" + "a".repeat(64), calls: [setCall(["1800000", "60000", "3600000", "86400000", 42])] }],
        "one fleet_settings::set with the budget, its owner cap, the clock and exactly the chosen values");
      // The budget page's estimate uses the same live quote and settles: no render loop.
      await page.getByTestId("budget-estimate").getByText(/Suggested [\d.]+ SUI/).waitFor();
      assert.match(await text("budget-estimate"), /rescaled to 1000 MIST gas \(testnet reference gas price \(live\)/);
      const renders = (await control()).budgetRenders;
      await page.waitForTimeout(1000);
      assert.equal((await control()).budgetRenders, renders, "the budget estimate keeps re-rendering");
      assert.ok(renders < 20, `budget estimate rendered ${renders} times`);
      assert.deepEqual(errors, []);
      await page.close();
    }

    // ── Enough deposit: no warning, signs directly ──
    {
      const { page, errors, waitText, control } = await open("funded");
      await waitText("The current deposit covers this plan.");
      await page.locator("#checkIntervalMs-900000").check();
      await page.getByRole("button", { name: "Save settings" }).click();
      await waitText("Settings saved in FAKEDIGEST1");
      const { signed } = await control();
      assert.ok(!(await page.textContent("body")).includes("Not enough deposit"));
      assert.deepEqual(signed.map((s) => s.calls), [[setCall(["900000", "60000", "86400000", "604800000", 42])]]);
      assert.deepEqual(errors, []);
      await page.close();
    }

    // ── Saved settings preselect their options and price from them ──
    {
      const { page, errors, waitText, control, text } = await open("saved");
      await waitText("Saved on chain: checks every 15 min, rollout fast checks off, health every 1 h, archives every 24 h kept 42 epoch(s).");
      for (const id of ["checkIntervalMs-900000", "rolloutCheckIntervalMs-0", "healthIntervalMs-3600000", "archiveIntervalMs-86400000", "archiveRetentionDays-42"]) {
        assert.ok(await page.locator(`#${id}`).isChecked(), `${id} is the saved choice`);
      }
      assert.match(await text("suifleet-price"), /add-ons \$0\.39/, "hourly health and 15-minute checks for 3 devices; daily archives not charged with log uploads off");
      await page.locator("#healthIntervalMs-86400000").check();
      await waitText("add-ons $0.03");
      await page.getByLabel(/Estimate with device log uploads/).check();
      await waitText("add-ons $0.27");
      assert.ok(await page.getByRole("button", { name: "Save settings" }).isEnabled());
      assert.equal((await control()).signed.length, 0);
      assert.deepEqual(errors, []);
      await page.close();
    }

    // ── Another fleet without remounting: its size, saved settings and a fresh confirmation ──
    {
      const { page, errors, waitText, control } = await open("switch");
      await waitText("Saved on chain: checks every 15 min");
      await page.locator("#healthIntervalMs-86400000").check();
      await page.getByLabel("Devices", { exact: true }).first().fill("1000");
      await page.getByRole("button", { name: "Save settings" }).click();
      await waitText("Not enough deposit for these settings");
      await page.evaluate(() => window.__switchFleet());
      await waitText("Saved on chain: checks every 5 min, rollout fast checks every 60 s, health every 24 h, archives every 24 h kept 42 epoch(s).");
      for (const id of ["checkIntervalMs-300000", "rolloutCheckIntervalMs-60000", "healthIntervalMs-86400000", "archiveIntervalMs-86400000", "archiveRetentionDays-42"]) {
        assert.ok(await page.locator(`#${id}`).isChecked(), `${id} is the second fleet's saved choice`);
      }
      assert.equal(await page.getByLabel("Devices", { exact: true }).first().inputValue(), "5", "sized from the second fleet's devices");
      assert.ok(!(await page.textContent("body")).includes("Not enough deposit"), "the first fleet's warning is withdrawn");
      assert.deepEqual((await control()).settingsReads.slice(-1), [BUDGET2]);
      await page.getByRole("button", { name: "Save settings" }).click();
      await waitText("Not enough deposit for these settings");
      await page.getByRole("button", { name: "Save anyway" }).click();
      await waitText("Settings saved in FAKEDIGEST1");
      assert.deepEqual((await control()).signed, [{ sender: "0x" + "e".repeat(64), calls: [setCall(["300000", "60000", "86400000", "86400000", 42], BUDGET2, CAP2)] }],
        "the second fleet's budget, cap and saved values; nothing carried over from the first");
      assert.deepEqual(errors, []);
      await page.close();
    }

    // ── A saved record that arrives after the owner chose does not overwrite the choice ──
    {
      const { page, errors, waitText } = await open("delayed");
      await page.locator("#healthIntervalMs-3600000").check();
      await page.evaluate(() => window.__releaseSaved());
      await waitText("Saved on chain: checks every 15 min");
      for (const id of ["healthIntervalMs-3600000", "checkIntervalMs-1800000"]) {
        assert.ok(await page.locator(`#${id}`).isChecked(), `${id} stays as the owner left it`);
      }
      assert.deepEqual(errors, []);
      await page.close();
    }

    // ── The wallet refuses: nothing is reported saved ──
    {
      const { page, errors, waitText, control } = await open("rejected");
      await page.getByRole("button", { name: "Save settings" }).click();
      await waitText("Not enough deposit for these settings");
      await page.getByRole("button", { name: "Save anyway" }).click();
      await waitText("User rejected the request. Nothing changed unless the transaction confirms; refresh before retrying.");
      assert.ok(!(await page.textContent("body")).includes("Settings saved in"));
      assert.equal((await control()).signed.length, 1);
      assert.deepEqual(errors, []);
      await page.close();
    }

    // ── A deployment without SF-130's settings type origins: prices shown, saving disabled, nothing read ──
    {
      const { page, errors, waitText, control } = await open("unsupported");
      await waitText("no on-chain fleet settings (SF-130)");
      assert.ok(await page.getByRole("button", { name: "Save settings" }).isDisabled());
      assert.deepEqual([(await control()).signed.length, (await control()).settingsReads.length], [0, 0]);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log("PASS: real browser Fleet settings page: per-option network cost and SuiFleet price per device and per fleet, dated live gas/Walrus quotes and "
      + "on-request USD quote, deposit follows settings, an underfunded save warns before signing and signs only the chosen settings after confirmation, "
      + "funded save signs directly, each save is the real SF-130 fleet_settings::set with the budget/cap/clock and chosen values, saved settings "
      + "preselect their options, switching fleets resets the form to the new fleet's size and saved settings and signs only its budget/cap, "
      + "a late saved-settings read keeps the owner's choice, a wallet refusal reports nothing saved, saving disabled when the manifest lacks the SF-130 type origins");
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise((resolve) => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
