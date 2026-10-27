// Uses a disposable Wallet Standard fixture; never funds, signs on a public chain,
// installs a wallet extension or reads the user's browser profile.
const fs = require("node:fs/promises"), fsSync = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const address = "0x" + "0".repeat(63) + "a";
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");
// Runs in the page: elements wider than the viewport, and visible text below WCAG AA contrast.
function audit() {
  const width = document.documentElement.clientWidth, overflow = [], contrast = [];
  if (document.documentElement.scrollWidth > width) overflow.push(`document ${document.documentElement.scrollWidth}px`);
  const canvas = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const rgba = (color) => { canvas.clearRect(0, 0, 1, 1); canvas.fillStyle = "#000"; canvas.fillStyle = color; canvas.fillRect(0, 0, 1, 1); return [...canvas.getImageData(0, 0, 1, 1).data]; };
  const luminance = ([r, g, b]) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }).reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  const background = (element) => { for (let e = element; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c[3] === 255) return c; } return [255, 255, 255, 255]; };
  for (const element of document.querySelectorAll("#root *")) {
    const box = element.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    if (box.right > width + 0.5 && !element.closest("details:not([open])")) overflow.push(`${element.tagName} ${element.textContent.slice(0, 40)} right=${Math.round(box.right)}`);
    const text = [...element.childNodes].some((node) => node.nodeType === 3 && node.textContent.trim());
    if (!text || element.closest(":disabled") || getComputedStyle(element).visibility === "hidden") continue;
    const style = getComputedStyle(element), size = parseFloat(style.fontSize), bold = Number(style.fontWeight) >= 700;
    const fg = luminance(rgba(style.color)), bg = luminance(background(element));
    const ratio = (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
    if (ratio < (size >= 24 || (bold && size >= 18.66) ? 3 : 4.5)) contrast.push(`${element.textContent.trim().slice(0, 40)} ${ratio.toFixed(2)}`);
  }
  return { overflow, contrast };
}

(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-wallet-browser-"));
  let server, browser;
  try {
    let manifest = JSON.parse(fsSync.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
    if (process.env.FLEET_TEST_NETWORK === "testnet") manifest = { ...manifest, network: "testnet", chain_identifier: "4c78adac" };
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/onboarding-acceptance.jsx")], bundle: true, platform: "browser", outfile: bundle, jsx: "automatic", nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") }, define: { "process.env": "{}", "process.env.NEXT_PUBLIC_ENOKI_API_KEY": JSON.stringify("enoki_public_fixture"), "process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID": JSON.stringify("fixture.apps.googleusercontent.com"), "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(manifest)), ACCEPTANCE_MANIFEST: JSON.stringify(manifest) }, logLevel: "warning" });
    // The application's real Tailwind stylesheet, so layout/contrast checks see production styles.
    const cssPath = path.resolve(__dirname, "../dapp/src/app/globals.css");
    const css = (await appRequire("postcss")([appRequire("@tailwindcss/postcss")({ base: path.resolve(__dirname, "../dapp") })]).process(await fs.readFile(cssPath, "utf8"), { from: cssPath })).css;
    server = http.createServer(async (req, res) => {
      const type = { "/app.js": "text/javascript", "/app.css": "text/css" }[req.url] ?? "text/html";
      res.setHeader("Content-Type", type);
      res.end(req.url === "/app.js" ? await fs.readFile(bundle) : req.url === "/app.css" ? css : '<!doctype html><html lang="en"><head><title>Wallet acceptance</title><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body class="bg-[#f0f2f5] p-4 md:p-8"><div id="root"></div><script src="/app.js"></script></body></html>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const page = await browser.newPage();
    await page.route("https://api.slush.app/api/wallet/metadata", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ enabled: true }) }));
    await page.route("https://api.enoki.mystenlabs.com/**", (route) => route.abort());
    const errors = []; page.on("pageerror", (error) => errors.push(error.message));
    const screenshots = process.env.SCREENSHOT_DIR;
    // Every width: no horizontal overflow, controls inside the viewport and WCAG AA text contrast.
    async function layout(name) {
      for (const width of [375, 768, 1280]) {
        await page.setViewportSize({ width, height: 900 });
        const result = await page.evaluate(audit);
        assert.deepEqual(result.overflow, [], `${name} ${width}px overflow`);
        assert.deepEqual(result.contrast, [], `${name} ${width}px contrast`);
        if (screenshots) await page.screenshot({ path: path.join(screenshots, `onboarding-${manifest.network}-${name}-${width}.png`), fullPage: true });
      }
      await page.setViewportSize({ width: 1280, height: 900 });
    }
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.getByText("Get started with SuiFleet", { exact: true }).waitFor();
    const register = page.getByRole("button", { name: "Register device", exact: true });
    const progress = page.getByRole("navigation", { name: "Onboarding progress" });
    const next = page.getByRole("status").filter({ hasText: "Next:" });
    await page.waitForFunction(() => window.acceptance.wallets().includes("SuiFleet Acceptance Wallet"));
    await page.waitForFunction(() => window.acceptance.wallets().some((name) => name.includes("Slush")));
    if (manifest.network === "testnet") await page.waitForFunction(() => window.acceptance.wallets().includes("Sign in with Google"));
    else assert.equal(await page.evaluate(() => window.acceptance.wallets().includes("Sign in with Google")), false);

    // Disconnected: one clear next action, later steps honestly waiting, no advanced operations.
    await progress.getByText("Not connected", { exact: true }).waitFor();
    await next.filter({ hasText: manifest.network === "testnet" ? "Sign in with Google" : "Connect Slush" }).waitFor();
    assert.equal(await progress.locator('[aria-current="step"]').count(), 1);
    assert.match(await progress.locator('[aria-current="step"]').innerText(), /Sign in/);
    await page.getByText("Connect your account and add funds first.", { exact: true }).waitFor();
    assert.equal(await page.getByText("Advanced: move fleet ownership").count(), 0);
    await page.getByText("You hold this fleet’s keys", { exact: true }).waitFor();
    await layout("disconnected");

    // Keyboard: the first tab stop is the progress navigation with a visible focus indicator.
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Sign in");
    assert.notEqual(await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle), "none");

    await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
    await page.getByRole("button", { name: "SuiFleet Acceptance Wallet", exact: false }).click();
    await page.getByText("Fund this address before registering.", { exact: false }).waitFor();
    await page.getByLabel("Network fee balance 0 SUI", { exact: true }).waitFor();
    await progress.getByText("Not funded", { exact: true }).waitFor();
    await next.filter({ hasText: "Add SUI to this account" }).waitFor();
    assert.equal(await register.isDisabled(), true);
    await page.getByText("SuiFleet Acceptance Wallet", { exact: true }).waitFor();
    await page.getByText(`Network: ${manifest.network}`, { exact: true }).waitFor();
    const technical = page.getByText("Technical details", { exact: true });
    await technical.click();
    await page.getByText("0 SUI (0 MIST)", { exact: true }).waitFor();
    await layout("zero-balance");

    // Ownership transfer is progressive disclosure with its warnings intact, and opens by keyboard.
    const handover = page.getByRole("link", { name: "Open fleet handover", exact: true });
    assert.equal(await handover.isVisible(), false);
    await page.getByText("Advanced: move fleet ownership", { exact: true }).focus();
    await page.keyboard.press("Enter");
    await handover.waitFor();
    assert.equal(await handover.getAttribute("href"), "/handover");
    await page.getByText("The recipient must confirm control", { exact: false }).waitFor();
    await page.getByText("Handover is resumable, not fleet-wide atomic", { exact: false }).waitFor();
    await page.getByText("verify the recipient’s authority", { exact: false }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Transfer owner capability" }).count(), 0);
    assert.equal(await page.getByRole("textbox", { name: "New owner address" }).count(), 0);

    await page.evaluate(() => window.acceptance.fund());
    await page.getByRole("button", { name: "Refresh account and inventory" }).click();
    await page.getByLabel("Network fee balance 1 SUI", { exact: true }).waitFor();
    await progress.getByText("Funds available", { exact: true }).waitFor();
    await page.getByText("1 SUI (1,000,000,000 MIST)", { exact: true }).waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll("button")].some((b) => b.textContent === "Register device" && !b.disabled));
    // The signing account and network stay visible next to the action.
    const signer = await page.getByText("You will sign as", { exact: false }).innerText();
    assert.ok(signer.includes(address) && signer.includes(manifest.network), signer);
    await page.getByRole("textbox", { name: "Device name", exact: true }).fill("First Pi");
    await page.getByRole("textbox", { name: /^Device public address/ }).fill("0x" + "2".repeat(64));
    await page.evaluate(() => window.acceptance.reject());
    await register.click();
    await page.getByRole("alert").filter({ hasText: "User rejected signing" }).waitFor();
    await page.evaluate(() => window.acceptance.reset());
    // A successful refresh replaces an earlier failure message with the current inventory.
    await page.getByRole("button", { name: "Refresh account and inventory" }).click();
    await page.getByRole("alert").filter({ hasText: "User rejected signing" }).waitFor({ state: "detached" });

    // Duplicate submission: a double activation while the wallet is open signs once.
    const beforeSuccess = await page.evaluate(() => window.acceptance.signCalls());
    await page.evaluate(() => { window.acceptance.hold(); window.acceptance.lag(); window.acceptance.registerAs("First Pi", "0x" + "2".repeat(64)); });
    await register.dblclick();
    await page.getByText("Approve the registration in your wallet…", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Registering…", exact: true }).click({ force: true }).catch(() => {});
    await page.evaluate(() => window.acceptance.release());
    await page.getByRole("status").filter({ hasText: "Device registered." }).waitFor();
    assert.equal(await page.evaluate(() => window.acceptance.signCalls()), beforeSuccess + 1);
    const confirmed = await page.getByRole("status").filter({ hasText: "Device registered." }).innerText();
    assert.match(confirmed, /not online until its client/);
    // Until the inventory lists it, the registration shows as pending and cannot be repeated.
    await page.getByRole("status").filter({ hasText: "First Pi: registered · adding to your inventory…" }).waitFor();
    assert.equal(await register.isDisabled(), true, "pending registration blocks a duplicate");
    await page.evaluate(() => window.acceptance.index());
    await page.getByRole("status").filter({ hasText: "adding to your inventory" }).waitFor({ state: "detached" });
    const confirmation = page.getByText("Confirmation details", { exact: true });
    await confirmation.click();
    await page.getByText("Confirmed transaction " + "1".repeat(32) + ".", { exact: false }).waitFor();
    await confirmation.click();
    await page.getByRole("listitem").filter({ hasText: /^First Pi$/ }).waitFor();
    await progress.getByText("1 registered", { exact: true }).waitFor();
    await next.filter({ hasText: "Deploy a component" }).waitFor();
    assert.equal(await page.getByRole("link", { name: "Deploy a component", exact: true }).getAttribute("href"), "/firmware");
    // Registration never claims installation or online health.
    assert.equal(await progress.getByText(/online|installed|healthy/i).count(), 0);
    // A confirmed registration clears the form so it cannot be resubmitted unchanged.
    assert.equal(await page.getByRole("textbox", { name: "Device name", exact: true }).inputValue(), "");
    await layout("registered");
    await page.getByRole("textbox", { name: "Device name", exact: true }).fill("Second Pi");
    await page.getByRole("textbox", { name: /^Device public address/ }).fill("0x" + "4".repeat(64));

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

    // Unavailable balance or inventory blocks registration instead of looking ready.
    await page.evaluate(() => { window.acceptance.reset(); window.acceptance.balanceFail(); });
    await page.getByRole("button", { name: "Refresh account and inventory" }).click();
    await page.getByRole("alert").filter({ hasText: "Balance service unavailable" }).waitFor();
    await progress.getByText("Balance unavailable", { exact: true }).waitFor();
    assert.equal(await register.isDisabled(), true);
    await page.evaluate(() => { window.acceptance.reset(); window.acceptance.inventoryFail(); });
    await page.getByRole("button", { name: "Refresh account and inventory" }).click();
    await page.getByRole("alert").filter({ hasText: "Inventory unavailable" }).waitFor();
    await progress.getByText("Inventory unavailable", { exact: true }).waitFor();
    assert.equal(await register.isDisabled(), true);
    await page.evaluate(() => window.acceptance.reset());
    await page.getByRole("button", { name: "Refresh account and inventory" }).click();
    await progress.getByText("1 registered", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.acceptance.signCalls()), before);

    await page.evaluate(() => window.acceptance.disconnect());
    await progress.getByText("Not connected", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log(`PASS: browser onboarding ${manifest.network} progress/next action, connect, readable balance with exact details, zero-gas gate, disclosed recipient-confirmed handover, keyboard focus, registration and inventory, duplicate-submit guard, rejection, wall-clock/epoch expiry, network mismatch, unavailable balance/inventory, 375/768/1280px layout and AA contrast, disconnect; fixture wallet/network only`);
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise((resolve) => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
