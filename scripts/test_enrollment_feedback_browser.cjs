// SF-277: the real Enrollment page warns before the only credential download,
// keeps lost-credential revoke/reissue guidance across a reload, never offers
// to reissue an enrolled device, and an unconfirmed or double-clicked
// revocation never sends a second wallet request. Chain reads and wallet
// approval are scripted fixtures; no real wallet or chain is used.
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");

(async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-enrollment-feedback-")); let server, browser;
  try {
    const root = path.resolve(__dirname, ".."), src = path.join(root, "dapp/src"), entry = path.join(__dirname, "browser/enrollment-feedback-fixture.jsx");
    const page = path.join(src, "app/enrollment/page.tsx");
    const manifest = await fs.readFile(path.join(root, "manifests/fixture.localnet.json"), "utf8");
    const postcss = appRequire("postcss"), tailwind = appRequire("@tailwindcss/postcss"), globals = path.join(src, "app/globals.css");
    await fs.writeFile(path.join(temp, "app.css"), (await postcss([tailwind({ base: path.join(root, "dapp") })]).process(await fs.readFile(globals, "utf8"), { from: globals })).css);
    await build({ entryPoints: [entry], bundle: true, platform: "browser", outfile: path.join(temp, "app.js"), jsx: "automatic", nodePaths: [path.join(root, "dapp/node_modules")], alias: { "@": src },
      plugins: [{ name: "enrollment-fixture", setup(b) {
        b.onResolve({ filter: /^(@mysten\/dapp-kit-react|@\/hooks\/(useCurrentAccount|useFleetSelection|useDevices|useGroups|useFleetTransaction))$/ }, () => ({ path: entry }));
        // Only the page's chain reads are scripted; the fixture itself re-exports the real module.
        b.onResolve({ filter: /^@\/lib\/enrollment$/ }, (args) => args.importer === page ? { path: entry } : undefined);
      } }],
      define: { "process.env": "{}", "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(manifest) }, logLevel: "warning" });
    server = http.createServer(async (req, res) => {
      const file = { "/app.js": ["app.js", "text/javascript"], "/app.css": ["app.css", "text/css"] }[req.url];
      res.setHeader("Content-Type", file ? file[1] : "text/html");
      res.end(file ? await fs.readFile(path.join(temp, file[0])) : '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"><div id="root" style="padding:16px"></div><script src="/app.js"></script>');
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    const origin = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const context = await browser.newContext({ acceptDownloads: true }), tab = await context.newPage(); tab.setDefaultTimeout(10000);
    const errors = [], dialogs = [];
    tab.on("pageerror", (e) => errors.push(e.message));
    tab.on("dialog", (d) => { dialogs.push(`${d.type()}: ${d.message()}`); void d.accept(); });
    const open = async () => {
      await tab.goto(origin);
      await tab.locator("#policy").selectOption({ index: 1 });
      await tab.getByTestId("status-counts").getByText("Waiting for device: 1").or(tab.getByTestId("status-counts").getByText("Revoked: 1")).first().waitFor();
    };
    const statusOf = async (serial) => (await tab.getByTestId("enrollment-entries").locator("tr", { hasText: serial }).locator("td").nth(3).innerText()).trim();
    const storedEntry = (serial) => tab.evaluate((s) => { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k.startsWith("suifleet.enrollment.")) { const job = JSON.parse(localStorage.getItem(k)); const e = job.entries.find((x) => x.serial === s); if (e) return e; } } return null; }, serial);
    await open();
    assert.equal(await statusOf("SN-1"), "Waiting for device"); assert.equal(await statusOf("SN-2"), "Enrolled"); assert.equal(await statusOf("SN-3"), "Reserved");

    // 1. The single-copy warning precedes the download, which needs an explicit acknowledgment.
    await tab.getByRole("tab", { name: "Pair one device" }).click();
    await tab.getByLabel("Alias").fill("pi-9"); await tab.getByLabel("Serial number").fill("SN-9");
    await tab.getByRole("button", { name: "Check", exact: true }).click();
    const download = tab.getByRole("button", { name: "Download credentials for 1 device" }), ack = tab.getByRole("checkbox", { name: "I understand this download is the only copy of these credentials" });
    const warning = tab.getByTestId("credential-warning");
    assert.match(await warning.innerText(), /Before you download: this file is the only copy of the claim keys/);
    assert.match(await warning.innerText(), /Lost a credential file\?/);
    assert.equal(await warning.evaluate((w, b) => !!(w.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING), await download.elementHandle()), true, "the warning comes before the download button");
    assert.equal(await download.isDisabled(), true, "download waits for acknowledgment");
    await ack.check();
    const [file] = await Promise.all([tab.waitForEvent("download"), download.click()]);
    assert.match(file.suggestedFilename(), /^suifleet-enrollment-.*\.tar$/); await file.delete();
    assert.equal(await ack.isChecked(), false, "each download needs its own acknowledgment");
    assert.equal(await tab.getByRole("button", { name: "Download credentials", exact: true }).isDisabled(), true);
    await tab.getByTestId("enrollment-entries").locator("tr", { hasText: "SN-9" }).waitFor();

    // 2. Lost-file recovery: only unused live claims are revoked; enrolled devices are never reissued.
    const lost = tab.getByTestId("lost-credentials");
    await lost.locator("summary").click();
    await tab.getByLabel("Lost credential serials").fill("SN-1, SN-2, SN-3, SN-404");
    const guidance = await lost.getByRole("status").innerText();
    assert.match(guidance, /Revoke first: SN-1\./);
    assert.match(guidance, /No live claim; ready to reissue: SN-3\./);
    assert.match(guidance, /Already enrolled; nothing to do: SN-2\. The device keeps its own key; do not issue a new credential\./);
    assert.match(guidance, /Not in this job: SN-404\./);
    assert.equal(await lost.getByRole("button").filter({ hasText: "SN-2" }).count(), 0);

    // Double-click with an unconfirmed outcome: one confirmation, one wallet request, guidance not to repeat.
    await tab.evaluate(() => { window.fx.fail = "uncertain"; });
    await lost.getByRole("button", { name: "Revoke 1 lost claim" }).dblclick();
    await tab.waitForFunction(() => !!window.fx.pending);
    assert.equal(dialogs.filter((d) => d.includes("Revoke 1 lost claim (SN-1)")).length, 1, "one confirmation");
    assert.match(dialogs.at(-1), /Enrolled devices and other claims are not affected/);
    await tab.evaluate(() => window.fx.release());
    const alert = tab.getByRole("alert").filter({ hasText: "Revoke lost claims 1/1 may have been submitted, but its result is not confirmed." });
    await alert.waitFor(); assert.match(await alert.innerText(), /Do not try again yet/);
    assert.deepEqual(await tab.evaluate(() => window.fx.calls), [["revoke_claim"]], "double-click sends one revocation");
    assert.equal((await storedEntry("SN-1")).revoked, true, "an interrupted revocation is never re-registered automatically");
    assert.equal(await statusOf("SN-1"), "Waiting for device", "no invented success");

    // 3. Restart: guidance and the revocation marker survive a reload.
    await open();
    assert.equal((await storedEntry("SN-1")).revoked, true);
    await tab.getByTestId("lost-credentials").locator("summary").click();
    await tab.getByLabel("Lost credential serials").fill("SN-1");
    await tab.getByTestId("lost-credentials").getByRole("button", { name: "Revoke 1 lost claim" }).click();
    await tab.waitForFunction(() => !!window.fx.pending); await tab.evaluate(() => window.fx.release());
    await tab.getByTestId("lost-credentials").getByText("No live claim; ready to reissue: SN-1.", { exact: false }).waitFor();
    assert.equal(await statusOf("SN-1"), "Revoked");
    await tab.getByTestId("lost-credentials").getByRole("button", { name: "Remove 1 reservation to reissue" }).click();
    await tab.getByTestId("enrollment-entries").locator("tr", { hasText: "SN-1" }).waitFor({ state: "detached" });
    assert.equal(await statusOf("SN-2"), "Enrolled", "the enrolled device is untouched");

    // Reissue: the same serial is accepted again and a new file can be downloaded.
    await tab.getByRole("tab", { name: "Pair one device" }).click();
    await tab.getByLabel("Alias").fill("pi-1"); await tab.getByLabel("Serial number").fill("SN-1");
    await tab.getByRole("button", { name: "Check", exact: true }).click();
    assert.match(await tab.getByTestId("reservation-check").innerText(), /^1 ready\./);
    await ack.check();
    const [again] = await Promise.all([tab.waitForEvent("download"), tab.getByRole("button", { name: "Download credentials for 1 device" }).click()]); await again.delete();
    assert.equal(await statusOf("SN-1"), "Reserved");

    // Phone width: the warning and recovery guidance fit without horizontal scrolling.
    await tab.setViewportSize({ width: 375, height: 740 });
    for (const element of [tab.getByTestId("credential-warning"), tab.getByTestId("lost-credentials")]) {
      const box = await element.boundingBox(); assert.ok(box && box.x >= 0 && box.x + box.width <= 376, "fits a phone viewport");
    }
    assert.deepEqual(await tab.evaluate(() => window.fx.calls), [["revoke_claim"]], "one deliberate retry after the reload; reissue needs no wallet request");
    assert.ok(dialogs.every((d) => d.startsWith("confirm")), `no browser alerts: ${dialogs}`);
    assert.deepEqual(errors, []);
    console.log("PASS: single-copy warning and acknowledgment before each credential download; lost-file revoke/reissue limited to unused claims, enrolled device never reissued; unconfirmed double-clicked revocation sends one request, keeps its marker and guidance across reload; confirmed revoke frees the serial for a new credential; desktop and phone width; fixture chain/wallet only");
  } finally { if (browser) await browser.close(); if (server) await new Promise((r) => server.close(r)); await fs.rm(temp, { recursive: true, force: true }); }
})().catch((e) => { console.error(e); process.exitCode = 1; });
