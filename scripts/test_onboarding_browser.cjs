// Uses a disposable Wallet Standard fixture; never funds, signs on a public chain,
// installs a wallet extension or reads the user's browser profile.
const fs = require("node:fs/promises"), fsSync = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");
(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-wallet-browser-"));
  let server, browser;
  try {
    let manifest = JSON.parse(fsSync.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
    if (process.env.FLEET_TEST_NETWORK === "testnet") manifest = { ...manifest, network: "testnet", chain_identifier: "4c78adac" };
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/onboarding-acceptance.jsx")], bundle: true, platform: "browser", outfile: bundle, jsx: "automatic", nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") }, define: { "process.env": "{}", "process.env.NEXT_PUBLIC_ENOKI_API_KEY": JSON.stringify("enoki_public_fixture"), "process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID": JSON.stringify("fixture.apps.googleusercontent.com"), "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(manifest)), ACCEPTANCE_MANIFEST: JSON.stringify(manifest) }, logLevel: "warning" });
    server = http.createServer(async (req, res) => {
      res.setHeader("Content-Type", req.url === "/app.js" ? "text/javascript" : "text/html");
      res.end(req.url === "/app.js" ? await fs.readFile(bundle) : '<!doctype html><html><head><title>Wallet acceptance</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const page = await browser.newPage();
    await page.route("https://api.slush.app/api/wallet/metadata", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: true }) }));
    await page.route("https://api.enoki.mystenlabs.com/**", (route) => route.abort());
    const errors = []; page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByText("Get started with SuiFleet", { exact: true }).waitFor();
    const register = page.getByRole("button", { name: "Register device", exact: true });
    await page.waitForFunction(() => window.acceptance.wallets().includes("SuiFleet Acceptance Wallet"));
    await page.waitForFunction(() => window.acceptance.wallets().some((name) => name.includes("Slush")));
    if (manifest.network === "testnet") await page.waitForFunction(() => window.acceptance.wallets().includes("Sign in with Google"));
    else assert.equal(await page.evaluate(() => window.acceptance.wallets().includes("Sign in with Google")), false);
    await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
    await page.getByRole("button", { name: "SuiFleet Acceptance Wallet", exact: false }).click();
    await page.getByText("Fund this address before registering.", { exact: false }).waitFor();
    assert.equal(await register.isDisabled(), true);
    const handover = page.getByRole("link", { name: "Open fleet handover", exact: true });
    assert.equal(await handover.getAttribute("href"), "/handover");
    await page.getByText("The recipient must confirm control", { exact: false }).waitFor();
    await page.getByText("Handover is resumable, not fleet-wide atomic", { exact: false }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Transfer owner capability" }).count(), 0);
    assert.equal(await page.getByRole("textbox", { name: "New owner address" }).count(), 0);

    await page.evaluate(() => window.acceptance.fund());
    await page.getByRole("button", { name: "Refresh account and inventory" }).click();
    await page.waitForFunction(() => !document.querySelector('button.bg-blue-600').disabled);
    await page.getByRole("textbox", { name: "Device name", exact: true }).fill("First Pi");
    await page.getByRole("textbox", { name: "Device public address", exact: true }).fill("0x" + "2".repeat(64));
    await page.evaluate(() => window.acceptance.reject());
    await register.click();
    await page.getByRole("alert").filter({ hasText: "User rejected signing" }).waitFor();
    await page.evaluate(() => window.acceptance.reset());
    await register.click();
    await page.getByRole("status").filter({ hasText: "Confirmed transaction" }).waitFor();
    const before = await page.evaluate(() => window.acceptance.signCalls());
    await page.evaluate(() => window.acceptance.expire());
    await register.click();
    await page.getByRole("alert").filter({ hasText: "session expired" }).waitFor();
    assert.equal(await page.evaluate(() => window.acceptance.signCalls()), before);
    await page.evaluate(() => { window.acceptance.reset(); window.acceptance.advanceEpoch(); });
    await register.click();
    await page.getByRole("alert").filter({ hasText: "session expired" }).waitFor();
    assert.equal(await page.evaluate(() => window.acceptance.signCalls()), before);
    await page.evaluate(() => { window.acceptance.reset(); window.acceptance.wrongChain(); });
    await register.click();
    await page.getByRole("alert").filter({ hasText: "chain identifier mismatch" }).waitFor();
    assert.equal(await page.evaluate(() => window.acceptance.signCalls()), before);
    await page.evaluate(() => window.acceptance.disconnect());
    await page.getByText("Not connected", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log(`PASS: browser onboarding ${manifest.network} connect, recipient-confirmed handover guidance, zero-gas gate, registration, rejection, wall-clock/epoch expiry, network mismatch and disconnect; fixture wallet/network only`);
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise((resolve) => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
