// Exercise the actual production export, not a separately bundled React harness.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("../dapp/node_modules/playwright");
const root = path.resolve(__dirname, "../dapp/out");
const routesRoot = path.resolve(__dirname, "../dapp/src/app");
async function routes(dir = routesRoot) {
  const found = [];
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) found.push(...await routes(path.join(dir, entry.name)));
    else if (entry.name === "page.tsx") found.push("/" + path.relative(routesRoot, dir).split(path.sep).filter(Boolean).join("/"));
  }
  return found.sort();
}
(async () => {
  let browser, negativeControl = false;
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
      const file = path.resolve(root, "." + pathname + (pathname.endsWith("/") ? "index.html" : ""));
      if (!file.startsWith(root + path.sep)) throw Error("Invalid path");
      const content = await fs.readFile(file);
      res.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : file.endsWith(".html") ? "text/html" : "application/octet-stream");
      res.end(negativeControl && file.endsWith(".html") ? content.toString().replace(">SuiFleet</span>", ">Hydration negative control</span>") : content);
    } catch { res.writeHead(404); res.end(); }
  });
  try {
    await fs.access(path.join(root, "index.html"));
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, args: ["--no-sandbox"] });
    const cases = [{ route: "/", connected: false, negative: true }];
    for (const connected of [false, true]) for (const route of await routes()) cases.push({ route, connected, negative: false });
    for (const { route, connected, negative } of cases) {
      negativeControl = negative;
      const failures = [];
      const context = await browser.newContext({ locale: "de-DE", timezoneId: "Pacific/Honolulu" });
      // Fail closed on external services; this regression spends no gas and uses no keys.
      await context.route("**/*", route => new URL(route.request().url()).hostname === "127.0.0.1" ? route.continue() : route.abort());
      if (connected) await context.addInitScript(() => {
        const address = "0x" + "a".repeat(64);
        const account = { address, publicKey: new Uint8Array(32), chains: ["sui:testnet", "sui:localnet"], features: ["sui:signTransaction", "sui:signAndExecuteTransaction"] };
        const refuse = async () => { throw Error("Hydration fixture cannot sign"); };
        const wallet = { version: "1.0.0", name: "Hydration Fixture", icon: "data:image/svg+xml;base64,PHN2Zy8+", chains: account.chains, accounts: [account], features: {
          "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
          "standard:disconnect": { version: "1.0.0", disconnect: async () => {} },
          "standard:events": { version: "1.0.0", on: () => () => {} },
          "sui:signTransaction": { version: "2.0.0", signTransaction: refuse },
          "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: refuse },
        } };
        window.addEventListener("wallet-standard:app-ready", event => event.detail.register(wallet));
        window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));
        localStorage.setItem("mysten-dapp-kit:selected-wallet-and-address", `Hydration Fixture:${address}:`);
      });
      const page = await context.newPage();
      page.on("pageerror", e => failures.push(`${route}: ${e.message}`));
      page.on("console", msg => { if (/hydrat|server rendered|didn.t match|Minified React error #(418|423|425)/i.test(msg.text())) failures.push(`${route}: ${msg.text()}`); });
      await page.goto(`http://127.0.0.1:${server.address().port}${route === "/" ? "/" : route + "/"}`);
      // Allow hydration, effects, lazy wallet controls and recovery errors to run.
      if (connected) {
        // The real wallet control must show the reconnected fixture account.
        await page.getByRole("button", { name: /0xaaaa/ }).first().waitFor();
      } else await page.getByRole("button", { name: "Connect Wallet", exact: true }).first().waitFor();
      await page.waitForTimeout(300);
      assert.ok(await page.locator("#main-content").textContent());
      if (negative) assert.ok(failures.some(message => /hydrat|Minified React error #418/i.test(message)), "Detector must catch a deliberately mismatched server tree");
      else assert.deepEqual(failures, [], `${route} hydration failed`);
      console.log(`Loaded ${negative ? "negative control: " : ""}${route} (${connected ? "reconnected fixture wallet" : "logged out"})`);
      await context.close();
    }
    console.log("PASS: every exported console route hydrates without browser errors");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
