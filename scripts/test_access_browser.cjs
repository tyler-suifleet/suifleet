// Chrome regression of the real page and transaction builders. Query/wallet
// hooks are fixtures; no public transaction or provider acceptance is claimed.
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");
(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "sf102-browser-"));
  let server, browser;
  try {
    const root = path.resolve(__dirname, ".."), src = path.join(root, "dapp/src");
    const m = JSON.parse(await fs.readFile(path.join(root, "manifests/fixture.localnet.json")));
    const protocol = JSON.parse(await fs.readFile(path.join(root, "interfaces/fleet-v1.json")));
    for (const name of protocol.optional_type_origins.seal_policy) m.type_origins[name] = m.call_package_id;
    const fixture = path.join(__dirname, "browser/access-fixtures.jsx");
    const postcss = appRequire("postcss"), tailwind = appRequire("@tailwindcss/postcss");
    const css = await postcss([tailwind({ base: path.join(root, "dapp") })]).process(await fs.readFile(path.join(src, "app/globals.css"), "utf8"), { from: path.join(src, "app/globals.css") });
    await fs.writeFile(path.join(dir, "app.css"), css.css);
    for (const mode of ["unsupported", "missing", "ready"]) {
      await build({ entryPoints: [path.join(__dirname, "browser/access-acceptance.jsx")], outfile: path.join(dir, `${mode}.js`), bundle: true, platform: "browser", jsx: "automatic",
        nodePaths: [path.join(root, "dapp/node_modules")], banner: { js: "var process = { env: {} };" },
        alias: { "@": src, "@mysten/dapp-kit-react": fixture, "@/hooks/useAccessPolicies": fixture, "@/hooks/useDevices": fixture, "@/hooks/useGroups": fixture, "@/hooks/useFleetTransaction": fixture },
        define: { ACCESS_ENABLED: String(mode !== "unsupported"), "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(m)),
          "process.env.NEXT_PUBLIC_SEAL_KEY_SERVERS": mode === "ready" ? JSON.stringify('[{"objectId":"0x1","weight":1}]') : "undefined", "process.env.NEXT_PUBLIC_SEAL_THRESHOLD": '"1"' }, logLevel: "warning" });
    }
    server = http.createServer(async (req, res) => {
      const filename = req.url === "/app.css" ? "app.css" : req.url.endsWith(".js") ? path.basename(req.url) : undefined;
      res.setHeader("Content-Type", filename?.endsWith("js") ? "text/javascript" : filename ? "text/css" : "text/html");
      res.end(filename ? await fs.readFile(path.join(dir, filename)) : `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"><div id="root" style="padding:16px"></div><script src="/${req.url.slice(1) || "ready"}.js"></script>`);
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const page = await browser.newPage(), errors = [];
    page.on("pageerror", e => errors.push(e.message));
    const visit = mode => page.goto(`http://127.0.0.1:${server.address().port}/${mode}`);
    const update = changes => page.evaluate(c => window.updateAccess(c), changes);
    await visit("unsupported");
    await page.getByRole("heading", { name: "Access policies", exact: true }).waitFor();
    await page.getByText("Connect your wallet using", { exact: false }).waitFor();
    await page.getByRole("heading", { name: "Access policies unavailable on this deployment" }).waitFor();
    await update({ connected: true });
    assert.equal(await page.getByRole("button", { name: "Create policy" }).count(), 0);
    await visit("missing"); await update({ connected: true });
    await page.getByRole("heading", { name: "Encryption key servers are not configured" }).waitFor();
    await page.getByText("No policies administered", { exact: false }).waitFor();
    await update({ state: "loading" }); await page.getByText("Loading policies you administer…").waitFor();
    await update({ state: "error" }); await page.getByRole("button", { name: "Retry policies" }).click();
    await page.getByText("No policies administered", { exact: false }).waitFor();
    await page.getByLabel("Policy name").fill("Plant releases");
    await update({ fail: true }); await page.getByRole("button", { name: "Create policy" }).click();
    await page.getByText("Wallet rejected the transaction", { exact: true }).waitFor();
    assert.equal(await page.getByLabel("Policy name").inputValue(), "Plant releases");
    await update({ fail: false }); await page.getByRole("button", { name: "Create policy" }).click();
    await page.waitForFunction(() => document.querySelector("input").value === "");
    await visit("ready"); await update({ connected: true, state: "policy" });
    const card = page.getByRole("region", { name: "Policy Production sensors" });
    await card.waitFor();
    assert.equal(await card.getByRole("option", { name: /Legacy unactivated/ }).count(), 0);
    await card.getByLabel("Activated device").selectOption({ label: "Granted sensor (0x000000…)" });
    assert.equal(await card.getByRole("button", { name: "Grant decryption" }).isDisabled(), true);
    await card.getByText("This recipient already has a grant.").waitFor();
    await card.getByLabel("Activated device").selectOption({ label: "Factory sensor (0x000000…)" });
    await update({ fail: true }); await card.getByRole("button", { name: "Grant decryption" }).click();
    await page.getByText("Wallet rejected the transaction", { exact: true }).waitFor();
    assert.notEqual(await card.getByLabel("Activated device").inputValue(), "");
    await update({ fail: false }); await card.getByRole("button", { name: "Grant decryption" }).click();
    await page.waitForFunction(() => window.accessCalls.length === 2);
    await card.getByLabel("Recipient type").selectOption("group");
    await card.getByLabel("Group", { exact: true }).selectOption({ label: "Factory floor (0x000000…)" });
    await card.getByRole("button", { name: "Grant decryption" }).click();
    await card.getByLabel("Recipient type").selectOption("account");
    await card.getByLabel("Account address", { exact: true }).fill("0xinvalid");
    assert.equal(await card.getByRole("button", { name: "Grant decryption" }).isDisabled(), true);
    await card.getByLabel("Account address", { exact: true }).fill("0x" + "ab".repeat(32));
    await card.getByRole("button", { name: "Grant decryption" }).click();
    await card.getByRole("button", { name: "Revoke device Granted sensor" }).click();
    page.once("dialog", d => d.dismiss()); await card.getByRole("button", { name: "Replace encryption key" }).click();
    assert.equal(await page.evaluate(() => window.accessCalls.length), 5);
    page.once("dialog", d => d.accept()); await card.getByRole("button", { name: "Replace encryption key" }).click();
    const calls = await page.evaluate(() => window.accessCalls.map(tx => tx.commands[0].MoveCall.function));
    assert.deepEqual(calls, ["grant_device", "grant_device", "grant_group", "grant_account", "revoke_device", "rotate_key_epoch"]);
    await card.getByLabel("Recipient type").selectOption("device");
    await update({ recipients: "loading" }); await card.getByText("Loading device choices…").waitFor();
    await update({ recipients: "error" }); await card.getByRole("button", { name: "Retry recipients" }).click();
    await update({ recipients: "empty" }); await card.getByText("No activated devices available.", { exact: false }).waitFor();
    await card.getByLabel("Recipient type").selectOption("group"); await card.getByText("No administered groups available.", { exact: false }).waitFor();
    await update({ recipients: "ready", pending: true });
    assert.equal(await card.getByRole("button", { name: "Replace encryption key" }).isDisabled(), true);
    await update({ pending: false });
    for (const width of [375, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `overflow at ${width}`);
    }
    assert.deepEqual(errors, []);
    console.log(`PASS Chrome ${browser.version()}: availability, loading/error/retry/empty, named create/device/group/account grants, validation, failed input preservation, revoke, rotation confirmation, pending, 375/1280px; fixture hooks only`);
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise(resolve => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
