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
    const manifest = JSON.parse(fsSync.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/wallet-acceptance.jsx")], bundle: true, platform: "browser", outfile: bundle, jsx: "automatic", nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") }, define: { "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(manifest)), ACCEPTANCE_MANIFEST: JSON.stringify(manifest) }, logLevel: "warning" });
    server = http.createServer(async (req, res) => {
      res.setHeader("Content-Type", req.url === "/app.js" ? "text/javascript" : "text/html");
      res.end(req.url === "/app.js" ? await fs.readFile(bundle) : '<!doctype html><html><head><title>Wallet acceptance</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const page = await browser.newPage();
    const errors = []; page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.waitForSelector("#sign");
    await page.waitForFunction(() => window.acceptance.wallets().includes("SuiFleet Acceptance Wallet"));
    await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
    await page.getByRole("button", { name: "SuiFleet Acceptance Wallet", exact: false }).click();
    await page.waitForFunction(() => document.querySelector("#account").textContent.startsWith("0x"));
    async function sign(expected) {
      await page.locator("#sign").click();
      await page.waitForFunction(() => !["pending", "idle"].includes(document.querySelector("#outcome").textContent));
      const outcome = await page.locator("#outcome").textContent(); assert.match(outcome, expected);
    }
    await sign(/^success$/);
    await page.evaluate(() => window.acceptance.reject()); await sign(/User rejected signing/);
    await page.evaluate(() => window.acceptance.effectsFail()); await sign(/error:/);
    await page.evaluate(() => window.acceptance.reset());
    const before = await page.evaluate(() => window.acceptance.signCalls());
    await page.evaluate(() => window.acceptance.wrongChain()); await sign(/chain identifier mismatch/);
    assert.equal(await page.evaluate(() => window.acceptance.signCalls()), before);
    await page.evaluate(() => window.acceptance.reset());
    await page.evaluate(() => window.acceptance.unsupportedChain()); await sign(/does not support/);
    assert.equal(await page.evaluate(() => window.acceptance.signCalls()), before);
    await page.evaluate(() => window.acceptance.disconnect());
    await page.waitForFunction(() => document.querySelector("#account").textContent === "disconnected");
    await sign(/Connect a wallet/);
    assert.deepEqual(errors, []);
    console.log("PASS: real browser dApp Kit/Wallet Standard connect, sign/effects, reject, chain mismatch and disconnect; fixture wallet only");
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise((resolve) => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
