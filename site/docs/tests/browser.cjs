// Optional local browser QA: no server, external traffic, credentials or wallet.
const { chromium } = require(process.cwd() + "/dapp/node_modules/playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");
(async () => {
  const out = path.resolve(process.argv[2]);
  const browser = await chromium.launch({ executablePath: "/usr/bin/google-chrome", headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, "https://docs.suifleet.io");
      const rel = url.pathname.endsWith("/") ? url.pathname + "index.html" : url.pathname;
      const file = path.resolve(out, "." + rel);
      assert.ok(file.startsWith(out + path.sep));
      const ext = path.extname(file);
      const types = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".json": "application/json", ".svg": "image/svg+xml", ".woff2": "font/woff2" };
      await route.fulfill({ body: await fs.readFile(file), contentType: types[ext] || "application/octet-stream" });
    });
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("https://docs.suifleet.io/install/");
      await page.getByRole("heading", { name: "Install the SuiFleet device client", exact: true }).waitFor();
      await page.evaluate(() => document.fonts.ready);
      assert.ok(await page.evaluate(() => document.fonts.check('16px Inter')));
      const logo = await page.locator('.brand img').boundingBox();
      assert.ok(Math.abs(logo.width / logo.height - 180 / 110) < 0.02);
      const overflow = await page.evaluate(() => [...document.querySelectorAll("*")].filter(el => el.getBoundingClientRect().right > innerWidth).map(el => [el.tagName, el.className, Math.round(el.getBoundingClientRect().right)]));
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), JSON.stringify({width, overflow}));
      await page.getByRole("searchbox").fill("provisioning media");
      await page.getByRole("status").filter({ hasText: "matching pages" }).waitFor();
      await page.locator("#search-results").getByRole("link", {name: "Enrollment and provisioning media"}).click();
      assert.equal(new URL(page.url()).pathname, "/enrollment/");
    }
    assert.deepEqual(errors, []);
    console.log("desktop/mobile install, search and navigation browser QA passed");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
