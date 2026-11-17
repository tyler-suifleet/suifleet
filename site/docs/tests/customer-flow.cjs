// Read-only browser regression across marketing and documentation build artifacts.
// Usage: node .../customer-flow.cjs MARKETING_BUILD DOCS_BUILD [SCREENSHOTS]
const { chromium } = require(process.env.SUIFLEET_PLAYWRIGHT_MODULE || process.cwd() + "/dapp/node_modules/playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");
(async () => {
  const roots = {
    "https://suifleet.io": path.resolve(process.argv[2]),
    "https://docs.suifleet.io": path.resolve(process.argv[3]),
  };
  const screenshots = process.argv[4];
  if (screenshots) await fs.mkdir(screenshots, { recursive: true });
  const browser = await chromium.launch({ executablePath: "/usr/bin/google-chrome", headless: true });
  try {
    const page = await browser.newPage();
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.ok(roots[url.origin], "Unexpected external request: " + url.origin);
      const rel = url.pathname.endsWith("/") ? url.pathname + "index.html" : url.pathname;
      const file = path.resolve(roots[url.origin], "." + rel);
      assert.ok(file.startsWith(roots[url.origin] + path.sep));
      const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json" };
      await route.fulfill({ body: await fs.readFile(file), contentType: types[path.extname(file)] || "application/octet-stream" });
    });
    for (const width of [390, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("https://suifleet.io/docs/");
      assert.ok(!(await page.locator("main").innerText()).includes("not live"));
      assert.equal(await page.locator("nav").first().getByRole("link", { name: "Docs", exact: true }).getAttribute("href"), "https://docs.suifleet.io/");
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      if (screenshots) await page.screenshot({ path: path.join(screenshots, `marketing-${width}.png`), fullPage: true });
      await page.getByRole("link", { name: "Install and connect your first device", exact: true }).click();
      assert.equal(page.url(), "https://docs.suifleet.io/install/");
      if (screenshots) await page.screenshot({ path: path.join(screenshots, `install-${width}.png`), fullPage: true });
      await page.getByRole("link", { name: "device client setup", exact: false }).first().click();
      assert.equal(new URL(page.url()).pathname, "/device-client/");
      const text = await page.locator("article").innerText();
      for (const label of ["Download connection settings", "Register by address", "device_object_id", "device_cap_id"])
        assert.ok(text.includes(label), label);
    }
    console.log("marketing → docs → device setup passed at 390/1280px (local artifacts, no wallet or remote requests)");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
