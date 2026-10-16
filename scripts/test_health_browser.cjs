// Device health (SF-019) in a real browser: the actual Devices page shows
// verified status, last seen, reasons and installed components from a real
// HTTP health service (CORS, bearer token), and claims nothing without it.
// Wallet and chain reads are fixtures; reports are signed with Mysten's SDK.
//   CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_health_browser.cjs
const fs = require("node:fs/promises"), fsSync = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { normalizeSuiAddress: id, toBase64 } = appRequire("@mysten/sui/utils");
const TOKEN = "t".repeat(40);

(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-health-browser-"));
  let page_server, health_server, browser;
  try {
    const m = JSON.parse(fsSync.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
    const key = Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(7)), signer = key.toSuiAddress();
    const now = Date.now();
    const devices = [
      { name: "pi-healthy", n: 1 }, { name: "pi-degraded", n: 2 }, { name: "pi-offline", n: 3 },
      { name: "pi-forged", n: 4 }, { name: "pi-silent", n: 5 }, { name: "pi-legacy", n: 6, activated: false },
    ].map((d) => ({ activated: true, ...d, id: id(`0xd${d.n}`), cap: id(`0xc${d.n}`) }));
    const component = (phase) => ({ name: "svc", version: "1.4.2", deployment_id: id("0xa1"), installed: true, supervised: true,
      phase, restarts: phase === "running" ? 0 : 3, last_exit: phase === "running" ? null : "exit status: 1", since_ms: now - 5000 });
    const snapshot = (d, observed, changes = {}) => ({
      schema: 1, chain_identifier: m.chain_identifier, registry: m.shared_objects.registry.id, device_id: d.id, signer,
      seq: observed, observed_ms: observed, interval_seconds: 60, daemon_version: "0.2.1", boot_id: null, started_ms: observed - 60000,
      dropped: 0, sync: { last_success_ms: observed, consecutive_failures: 0, last_error: null }, pending_receipts: 0,
      components: [component("running")], health: "healthy", reasons: [], ...changes });
    async function report(d, s, received, by = key) {
      const bytes = new TextEncoder().encode(JSON.stringify(s));
      return { device_id: d.id, seq: s.seq, signer, observed_ms: s.observed_ms, received_ms: received,
               message: toBase64(bytes), signature: (await by.signPersonalMessage(bytes)).signature };
    }
    const [healthy, degraded, offline, forged] = devices;
    const items = [
      { device_id: healthy.id, report: await report(healthy, snapshot(healthy, now - 2000), now - 2000) },
      { device_id: degraded.id, report: await report(degraded, snapshot(degraded, now - 3000, {
        components: [component("backoff")], health: "degraded", reasons: ["component svc: backoff (exit status: 1)"] }), now - 3000) },
      { device_id: offline.id, report: await report(offline, snapshot(offline, now - 3600000), now - 3600000) },
      { device_id: forged.id, report: await report(forged, snapshot(forged, now - 1000), now - 1000, Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(8))) },
      { device_id: devices[4].id, report: null },
    ];
    const requests = [];
    let origin;
    health_server = http.createServer((req, res) => {
      requests.push({ url: req.url, method: req.method, authorization: req.headers.authorization });
      if (req.headers.origin === origin) {
        res.setHeader("Access-Control-Allow-Origin", origin);
        res.setHeader("Access-Control-Allow-Headers", "authorization, content-type");
      }
      if (req.method === "OPTIONS") { res.statusCode = 204; return res.end(); }
      if (req.headers.authorization !== `Bearer ${TOKEN}`) { res.statusCode = 401; return res.end('{"error":"missing, unknown or insufficient bearer token"}'); }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ items, next: null, server_time_ms: Date.now() }));
    });
    await new Promise((resolve) => health_server.listen(0, "127.0.0.1", resolve));
    const telemetry = `http://127.0.0.1:${health_server.address().port}`;
    const operator = id("0x0a");
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/health-acceptance.jsx")], bundle: true, platform: "browser", outfile: bundle,
      // next/link (imported by the shared UI module, not rendered here) reads process.env at load.
      jsx: "automatic", banner: { js: "var process = { env: {} };" }, nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") },
      define: { "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(m)),
                "process.env.NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL": JSON.stringify(telemetry),
                "process.env.NEXT_PUBLIC_WALRUS_EPOCHS": "undefined", "process.env.NEXT_PUBLIC_WALRUS_MAX_COST_FROST": "undefined",
                ACCEPTANCE_MANIFEST: JSON.stringify(m), HEALTH_FIXTURE: JSON.stringify({ operator, signer, devices }) }, logLevel: "warning" });
    page_server = http.createServer(async (req, res) => {
      res.setHeader("Content-Type", req.url === "/app.js" ? "text/javascript" : "text/html");
      res.end(req.url === "/app.js" ? await fs.readFile(bundle) : '<!doctype html><html><head><title>Health acceptance</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>');
    });
    await new Promise((resolve) => page_server.listen(0, "127.0.0.1", resolve));
    origin = `http://127.0.0.1:${page_server.address().port}`;
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const page = await browser.newPage();
    const errors = []; page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(origin);
    await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
    await page.getByRole("button", { name: "SuiFleet Acceptance Wallet", exact: false }).click();
    const row = (name) => page.locator("tr", { has: page.getByText(name, { exact: true }) });
    const status = async (name) => (await row(name).locator("td").first().textContent()).trim();
    await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 6);

    // No token: the service refuses (401) and no health is claimed.
    await page.waitForFunction(() => document.body.textContent.includes("Health unavailable"));
    for (const d of devices.slice(0, 5)) assert.equal(await status(d.name), "Health unavailable", d.name);
    assert.equal(await status("pi-legacy"), "Not activated");
    assert.ok(requests.some((r) => r.method === "GET" && r.url.startsWith(`/v1/health?owner=${operator}`) && !r.authorization));

    // With the read token: verified statuses.
    await page.getByPlaceholder("Read token").fill(TOKEN);
    await page.getByRole("button", { name: "Use", exact: true }).click();
    await page.waitForFunction(() => document.body.textContent.includes("Healthy"));
    assert.deepEqual(await Promise.all(devices.map((d) => status(d.name))),
      ["Healthy", "Degraded", "Offline", "Unverified", "Unreported", "Not activated"]);
    assert.match((await row("pi-healthy").locator("td").nth(3).textContent()).trim(), /^\d+ s ago$/);
    assert.equal((await row("pi-offline").locator("td").nth(3).textContent()).trim(), "60 min ago");
    assert.equal((await row("pi-silent").locator("td").nth(3).textContent()).trim(), "never");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("suifleet.telemetryToken")), "t".repeat(40));
    assert.ok(requests.some((r) => r.authorization === `Bearer ${TOKEN}`));

    // Details: reasons and the installed inventory.
    await row("pi-degraded").getByRole("button").first().click();
    await page.waitForFunction(() => document.body.textContent.includes("component svc: backoff (exit status: 1)"));
    const details = await page.getByTestId("health-details").textContent();
    for (const text of ["svc", "1.4.2", "backoff", "exit status: 1", "daemon 0.2.1", "verified against its on-chain identity"]) {
      assert.ok(details.includes(text), `details lack ${text}: ${details}`);
    }
    await row("pi-forged").getByRole("button").first().click();
    await page.waitForFunction(() => document.body.textContent.includes("newest report rejected: signature is not the device identity's"));
    assert.ok(!(await page.getByTestId("health-details").textContent()).includes("1.4.2"), "an unverified report's inventory was shown");

    // Forgetting the token: back to claiming nothing.
    await page.getByRole("button", { name: "Forget read token" }).click();
    await page.waitForFunction(() => !document.body.textContent.includes("Healthy"));
    await page.waitForFunction(() => document.body.textContent.includes("Health unavailable"));
    assert.deepEqual(errors, []);
    console.log("PASS: real browser Devices page: health unavailable without a token; with it Healthy/Degraded/Offline/Unverified/"
      + "Unreported verified per device, last seen, reasons and installed components; forged inventory hidden; fixture wallet and chain reads");
  } finally {
    if (browser) await browser.close();
    for (const server of [page_server, health_server]) if (server) await new Promise((resolve) => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
