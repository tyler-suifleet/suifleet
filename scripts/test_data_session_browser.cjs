// SF-084 Fleet archives page in a real browser: decrypted plaintext lives only
// as long as the signed Seal session that decrypted it. The real page renders
// over controlled chain/Seal/Walrus dependencies (scripts/browser/data-session-fakes.js)
// so an archive read can be held open while that session expires.
//   CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/test_data_session_browser.cjs
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");

const page_ = path.resolve(__dirname, "../dapp/src/app/data/page.tsx"), fakes = path.resolve(__dirname, "browser/data-session-fakes.js");
const faked = new Set(["@mysten/dapp-kit-react", "@mysten/seal", "@mysten/walrus", "@/hooks/useDevices", "@/hooks/useFleetTransaction", "@/hooks/useFleetDataConfig", "@/hooks/useFleetSelection", "@/lib/fleet-data",
  "@/components/FleetDataBrowser", "@/components/DataUsage", "@/components/StreamRetention", "@/lib/stream-retention", "@/lib/fleet-bulk", "@/lib/fleet-budget", "@/lib/deployment-manifest", "@/lib/seal", "@/lib/constants"]);
const fakeDependencies = { name: "data-session-fakes", setup(b) {
  b.onResolve({ filter: /.*/ }, (args) => args.importer === page_ && faked.has(args.path) ? { path: fakes } : undefined);
} };

(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-data-session-"));
  let server, browser;
  try {
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/data-session.jsx")], bundle: true, platform: "browser", outfile: bundle, jsx: "automatic",
      banner: { js: "var process = { env: {} };" }, nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") },
      define: { "process.env.NODE_ENV": '"development"' }, plugins: [fakeDependencies], logLevel: "warning" });
    server = http.createServer(async (req, res) => {
      const js = req.url === "/app.js";
      res.setHeader("Content-Type", js ? "text/javascript" : "text/html");
      res.end(js ? await fs.readFile(bundle) : '<!doctype html><html><head><title>Data session</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const page = await browser.newPage(), errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.clock.install();
    await page.goto(`http://127.0.0.1:${server.address().port}/data?device=${"0x" + "d1".padStart(64, "0")}`);
    const body = () => page.textContent("body");
    const waitText = (value) => page.waitForFunction((v) => document.body.textContent.includes(v), value, { timeout: 30_000 })
      .catch(async (error) => { throw new Error(`${error.message}; waiting for ${value}; page shows: ${await body()}`); });
    const openButton = page.getByRole("button", { name: "Open current shadow documents" });
    const settle = async () => { for (let i = 0; i < 5; i++) await page.evaluate(() => new Promise((r) => setTimeout(r, 0))); };
    // A device-signed schema-2 archive whose shadow timestamp is beyond the Date range.
    const resolveRead = (n) => page.evaluate((n) => window.__control.reads[n]({ schema: 2, kind: "fleet-data", seq: 9, observed_ms: 1_700_000_000_000, reason: "logs", dropped_lines: 0,
      records: [{ source: "daemon", line: "SECRET-LOG-LINE" }], shadows: { omitted: 0, documents: [{ name: "", version: 1, updated_ms: 9007199254740991,
        desired: { mode: "eco" }, reported: { mode: "eco" }, delta: {} }] } }), n);
    const expireKey = (n) => page.evaluate((n) => { window.__control.keys[n].expired = true; }, n);
    const tick = () => page.clock.runFor(5_000);
    const reads = () => page.evaluate(() => window.__control.reads.length);

    await waitText("granted support account");
    await openButton.waitFor();
    await page.getByText("Storage details", { exact: true }).click();
    await waitText("Originally purchased through storage period (Walrus epoch) 100");

    // A read completed within its session renders, including an out-of-range timestamp shown raw.
    await openButton.click();
    await page.waitForFunction(() => window.__control.reads.length === 1);
    await resolveRead(0);
    await waitText("SECRET-LOG-LINE");
    await waitText("updated UTC milliseconds 9007199254740991 (out of range)");
    await page.getByLabel("Search").fill("SECRET-LOG");
    // Expiry discards the plaintext and the search query that may hold copied log text.
    await expireKey(0);
    await tick();
    await waitText("Your secure viewing session expired. Open the archive again to continue.");
    assert.ok(!(await body()).includes("SECRET-LOG"), "expired plaintext and query discarded");

    // A read still pending when its session expires never publishes, and later ticks do not resurrect it.
    await openButton.click();
    await page.waitForFunction(() => window.__control.reads.length === 2);
    await expireKey(1);
    await tick();
    await waitText("Your secure viewing session expired");
    await resolveRead(1);
    await settle();
    for (let i = 0; i < 3; i++) await tick();
    assert.ok(!(await body()).includes("SECRET-LOG-LINE"), "late completion after expiry must not render plaintext");
    assert.equal(await openButton.isDisabled(), false, "the page is usable after the discarded read");

    // Expiry that the timer has not yet observed is checked immediately before publishing.
    await openButton.click();
    await page.waitForFunction(() => window.__control.reads.length === 3);
    assert.equal(await page.evaluate(() => window.__control.keys.length), 3, "each expired session is replaced by a newly signed one");
    await expireKey(2);
    await resolveRead(2);
    await waitText("Your secure viewing session expired");
    await settle();
    assert.ok(!(await body()).includes("SECRET-LOG-LINE"), "a read finishing after expiry must not render plaintext");

    // A fresh session opens normally with an empty search.
    await openButton.click();
    await page.waitForFunction(() => window.__control.reads.length === 4);
    await resolveRead(3);
    await waitText("SECRET-LOG-LINE");
    assert.equal(await page.getByLabel("Search").inputValue(), "");
    const record = await page.getByTestId("access-record").textContent();
    assert.equal((record.match(/opened · support/g) || []).length, 2, record);
    assert.equal((record.match(/refused · support/g) || []).length, 2, record);
    assert.equal(await reads(), 4);
    assert.deepEqual(await page.evaluate(() => [sessionStorage.length, localStorage.length]), [0, 0], "nothing persisted in web storage");
    // A selected-fleet change invalidates both rendered plaintext and an in-flight read.
    await openButton.click();
    await page.waitForFunction(() => window.__control.reads.length === 5);
    await page.evaluate(() => {
      window.__control.fleet = 'fleet-b';
      window.dispatchEvent(new Event('fixture-fleet-change'));
    });
    await settle();
    assert.ok(!(await body()).includes('SECRET-LOG-LINE'), 'fleet switch discards plaintext');
    await resolveRead(4);
    await settle();
    assert.ok(!(await body()).includes('SECRET-LOG-LINE'), 'old fleet read cannot publish into new scope');
    assert.deepEqual(errors, []);
    console.log("PASS: real browser Fleet archives page: plaintext and search query discarded at Seal session expiry; reads pending across expiry "
      + "(observed by the timer or not) never publish or resurrect plaintext; out-of-range shadow timestamp shown raw; fresh session reopens");
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise((resolve) => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
