// SF-084 dashboard in a real browser: Device services, alert drill-down,
// Rollouts and archive search/role gating over the real SF-082 verified path.
// Wallet and fullnode are fixtures serving exact BCS (localnet manifest);
// health reports are signed with Mysten's SDK; a forged report must hide values.
//   CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/test_dashboard_browser.cjs
const fs = require("node:fs/promises"), fsSync = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { normalizeSuiAddress: id } = appRequire("@mysten/sui/utils");

(async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-dashboard-browser-"));
  let server, browser;
  try {
    const m = JSON.parse(fsSync.readFileSync(path.resolve(__dirname, "../manifests/fixture.localnet.json")));
    const data = { schema: 1, chainIdentifier: m.chain_identifier, registryId: m.shared_objects.registry.id, packageId: id("0x81"),
      typeOrigin: id("0x81"), fleetId: id("0xf1"), budgetId: id("0xb1"), storageId: id("0x51") };
    const key = Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(7)), signer = key.toSuiAddress();
    const other = Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(8));
    const now = Date.now(), operator = id("0x0a");
    const services = (consecutive) => ({ version: 2,
      messaging: { subscriptions: 2, queued: 5, refused: 0, dropped: 0, omitted: 0, queues: [
        { principal: "sink", subscriptions: 1, queued: 5, fill_percent: 50, full_since_ms: null, refused: 0, refused_recent: 0, dropped: 0, dropped_recent: 0 }] },
      bridge: { connected: true, since_ms: now - 600_000, connects: 3, refused: 0, refused_recent: 0, rejected: 0, rejected_recent: 0, last_error: null },
      streams: { omitted: 0, streams: [{ name: "events", bytes: 400, max_size: 1000, fill_percent: 40, messages: 12, oldest: 30, newest: 41, overwrite: false, rejected: 0, rejected_recent: 0,
        export: { checkpoint: 37, lag: 4, lost: 0, lost_recent: 0, batches: 7, failures: consecutive, consecutive_failures: consecutive, last_success_ms: now - 600_000, last_error: consecutive ? "http-server-error" : null } }] },
      shadows: { shadows: 3, notifications_refused: 0, notifications_refused_recent: 0, omitted: 0,
        sync: [{ name: "config", conflicts: 1, conflicts_recent: 0, failures: 0, consecutive_failures: 0, last_success_ms: now - 30_000, last_error: null }] } });
    async function report(device, svc, by = key) {
      const snapshot = { schema: 2, chain_identifier: m.chain_identifier, registry: m.shared_objects.registry.id, device_id: device, signer, seq: 4,
        observed_ms: now - 10_000, interval_seconds: 60, daemon_version: "0.9.0", boot_id: null, started_ms: now - 3_600_000, dropped: 0,
        sync: { last_success_ms: now - 10_000, consecutive_failures: 0, last_error: null }, pending_receipts: 0, components: [], health: "healthy", reasons: [], services: svc };
      const bytes = new TextEncoder().encode(JSON.stringify(snapshot));
      return { seq: 4, atMs: now - 5_000, message: Array.from(bytes), signature: Array.from(Buffer.from((await by.signPersonalMessage(bytes)).signature, "base64")) };
    }
    const d1 = id("0xd1"), d2 = id("0xd2");
    const devices = [
      { id: d1, name: "pi-services", cap: id("0xc1"), writer: id("0xe1"), report: await report(d1, services(3)) },
      { id: d2, name: "pi-forgeable", cap: id("0xc2"), writer: id("0xe2"), report: await report(d2, services(0)), forgeable: true, forged: await report(d2, services(0), other) },
    ];
    const members = [id("0x101"), id("0x102"), id("0x103"), id("0x104")];
    const rollout = { id: id("0xab"), members, receipts: [{ key: members[0], value: 1 }, { key: members[1], value: 2 }],
      record: { policy: { concurrency: "2", rate_count: "2", rate_window_ms: "60000", bake_ms: "300000", minimum_samples: "2", failure_bps: "2500", offline_timeout_ms: "600000" },
        members, cohorts: ["0", "0", "1", "1"], admitted_at: ["100", "100", "0", "0"], outcomes: [1, 2, 0, 0], skipped: [false, false, false, false],
        cohort: "0", state: 2, bake_started_ms: "0", progress_ms: "300", revision: "3" },
      decisions: [{ revision: "1", cohort: "0", state: 0, reason: "created; no members admitted", at_ms: "1", admitted: [] },
        { revision: "2", cohort: "0", state: 0, reason: "admitted online cohort members", at_ms: "100", admitted: members.slice(0, 2) },
        { revision: "3", cohort: "0", state: 2, reason: "failure threshold reached; permanently aborted", at_ms: "300", admitted: [] }] };
    const archive = { records: [{ source: "daemon", line: "boot ok" }, { source: "component:app", line: "ERROR disk full" },
      { source: "component:app", line: "error retry" }, { source: "daemon", line: "shutdown" }], reason: "diagnostics", seq: 9, dropped_lines: 0 };
    // A device-signed schema-2 archive with shadow documents; the page runs the real verifier on it.
    const shadowArchive = { schema: 2, kind: "fleet-data", chain_identifier: m.chain_identifier, registry: m.shared_objects.registry.id, fleet_id: data.fleetId,
      device_id: d1, signer, seq: 9, observed_ms: now - 60_000, reason: "logs", dropped_lines: 0, records: [{ source: "daemon", line: "Shadow state snapshot" }],
      shadows: { omitted: 1, documents: [
        { name: "", version: 4, updated_ms: now - 120_000, desired: { mode: "eco", fan: { speed: 2 } }, reported: { mode: "eco", fan: { speed: 1 } }, delta: { fan: { speed: 2 } } },
        { name: "big", version: 1, updated_ms: now - 120_000, desired: null, reported: { count: 18446744073709551615n }, delta: {} },
        { name: "config", version: 7, updated_ms: now - 90_000, desired: null, reported: { firmware: "<b>3.1</b>" }, delta: {} }] } };
    const shadowMessage = new TextEncoder().encode(JSON.stringify(shadowArchive, (k, v) => typeof v === "bigint" ? `__BIG${v}__` : v).replace(/"__BIG(\d+)__"/g, "$1"));
    const shadowEnvelope = JSON.stringify({ message: Buffer.from(shadowMessage).toString("base64"), signature: (await key.signPersonalMessage(shadowMessage)).signature });
    const tamperedEnvelope = JSON.stringify({ ...JSON.parse(shadowEnvelope), message: Buffer.from(new TextDecoder().decode(shadowMessage).replace('"speed":1', '"speed":2')).toString("base64") });
    const shadowAnchor = { seq: "9", kind: 0, blob_object_id: id("0x77"), blob_id: "1", ciphertext_sha256: [], signer, at_ms: String(now - 30_000), end_epoch: 100, key_epoch: "0" };
    const access = [{ atMs: now, account: operator, authority: "support", device: d1, index: 3, kind: "diagnostics", outcome: "opened" }];
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/dashboard-acceptance.jsx")], bundle: true, platform: "browser", outfile: bundle,
      jsx: "automatic", banner: { js: "var process = { env: {} };" }, nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") },
      define: { "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(m)),
        "process.env.NEXT_PUBLIC_FLEET_DATA": JSON.stringify(JSON.stringify(data)), "process.env.NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL": '""',
        "process.env.NEXT_PUBLIC_WALRUS_EPOCHS": "undefined", "process.env.NEXT_PUBLIC_WALRUS_MAX_COST_FROST": "undefined",
        ACCEPTANCE_MANIFEST: JSON.stringify(m), DASHBOARD_FIXTURE: JSON.stringify({ operator, signer, devices, rollout, archive, access, shadowEnvelope, tamperedEnvelope, shadowAnchor }) }, logLevel: "warning" });
    server = http.createServer(async (req, res) => {
      const js = req.url === "/app.js";
      res.setHeader("Content-Type", js ? "text/javascript" : "text/html");
      res.end(js ? await fs.readFile(bundle) : '<!doctype html><html><head><title>Dashboard acceptance</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const errors = [];
    async function open(url) {
      const page = await browser.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(origin + url);
      await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
      await page.getByRole("button", { name: "SuiFleet Acceptance Wallet", exact: false }).click();
      return page;
    }
    // Product pages must work without a telemetry URL or a read token.
    for (const route of ["/devices", "/"]) {
      const product = await open(route);
      await product.waitForFunction(() => document.body.textContent.includes("pi-services"));
      if (route === "/devices") {
        await product.waitForFunction(() => document.body.textContent.includes("Healthy"));
        assert.equal(await product.getByPlaceholder("Read token").count(), 0);
      } else {
        await product.waitForFunction(() => document.body.textContent.includes("export failed 3 times")).catch(async error => { throw Error(error.message + "; page: " + await product.textContent("body")); });
      }
      assert.ok(!(await product.textContent("body")).includes("indexer read token"));
      assert.equal(await product.evaluate(() => sessionStorage.length), 0);
      assert.ok(await product.evaluate(() => Object.keys(localStorage).every(k => k.startsWith("suifleet-history-v1:"))));
      await product.close();
    }
    const text = (page, testId) => page.getByTestId(testId).textContent();
    const waitText = (page, value) => page.waitForFunction((v) => document.body.textContent.includes(v), value, { timeout: 30_000 })
      .catch(async (error) => { throw new Error(`${error.message}; waiting for ${value}; page shows: ${await page.textContent("body")}`); });

    // Verified services and the device's alert drill-down.
    let page = await open(`/services?device=${d1}`);
    await waitText(page, "Export checkpoint: sequence 37; 4 messages behind");
    const view = await text(page, "device-services");
    for (const expected of ["pi-services", "Healthy", "3 shadows", "config", "in the device's encrypted fleet archive", "Open shadow documents",
      "400 of 1000 bytes (40%), 12 messages", "Oldest sequence 30, newest 41.", "3 failures, 3 consecutive", "http-server-error",
      "Pub/sub: 2 subscriptions, 5 queued", "sink: 5 queued (50% full)", "MQTT bridge: connected"]) assert.ok(view.includes(expected), `services view lacks ${expected}: ${view}`);
    await waitText(page, "export-failing");
    const alerts = await text(page, "device-alerts");
    assert.ok(alerts.includes("critical") && alerts.includes("events export failed 3 times: http-server-error"), alerts);
    assert.ok(!alerts.includes("pi-forgeable"));
    await page.getByRole("link", { name: "Logs and diagnostics" }).first().getAttribute("href").then((href) => assert.match(href, new RegExp(`^/data/?\\?device=${d1}$`)));
    await page.getByRole("link", { name: "Open shadow documents" }).getAttribute("href").then((href) => assert.match(href, new RegExp(`^/data/?\\?device=${d1}$`)));
    await page.close();

    // A forged report for another device: no values, and fleet alerts are unavailable rather than partial.
    page = await open(`/services?device=${d2}#forged`);
    await waitText(page, "Service health unavailable.");
    await waitText(page, "Alerts unavailable.");
    const hidden = await text(page, "device-services");
    for (const leaked of ["Pub/sub:", "MQTT bridge", "Export checkpoint", "3 shadows"]) assert.ok(!hidden.includes(leaked), `forged report leaked ${leaked}: ${hidden}`);
    assert.ok(hidden.includes("signature is not the device identity's"), hidden);
    await page.close();

    // Disconnected operators retain read-only rollout controls and cannot write.
    const disconnected = await browser.newPage();
    await disconnected.goto(origin + "/rollouts");
    await waitText(disconnected, "You can still view available rollout records.");
    await disconnected.getByText("Create a gradual rollout", { exact: true }).click();
    assert.equal(await disconnected.getByRole("button", { name: "Create without admitting devices", exact: true }).isDisabled(), true);
    await disconnected.getByRole("combobox", { name: /^Rollout/ }).waitFor();
    await disconnected.close();

    // Fleet rollout overview and the aborted rollout's stages and audit.
    page = await open("/rollouts");
    await page.waitForFunction(() => document.querySelector('[data-testid="rollout-overview"]')?.textContent.includes("Aborted"));
    const overview = await text(page, "rollout-overview");
    for (const expected of ["app 2.0.0", "Aborted", "1 of 2", "2 of 4", "1 / 1", "25% after 2 samples · reached"]) assert.ok(overview.includes(expected), `overview lacks ${expected}: ${overview}`);
    await page.getByRole("button", { name: "app 2.0.0" }).click();
    await waitText(page, "Aborted permanently: failure threshold reached; permanently aborted");
    const stages = await page.getByTestId("rollout-stages").locator("tbody tr").allTextContents();
    assert.deepEqual(stages, ["1Current2201100", "2Pending2000002"]);
    await page.getByText("Decision audit").click();
    const detail = await text(page, "rollout-detail");
    for (const expected of ["#3", "#2", "#1", "granted 2", "2 settled, 1 failed (50%); failure threshold reached"]) assert.ok(detail.includes(expected), `detail lacks ${expected}: ${detail}`);
    await page.close();

    // Active advance reaches the wallet using only chain state: no indexer
    // token, 120-second telemetry snapshot or periodic fleet-data read exists.
    page = await open("/rollouts#onchain");
    await page.getByRole("button", { name: "app 2.0.0" }).click();
    await page.getByLabel("Admission candidate IDs").fill(members[2]);
    await page.getByRole("button", { name: "Check device health and continue" }).click();
    await waitText(page, "Fixture wallet stopped before signing");
    const submitted = await page.evaluate(() => window.rolloutTransaction);
    assert.deepEqual(submitted.commands.map(c => c.MoveCall.function), ["advance_rollout_onchain"]);
    const candidateArg = submitted.commands[0].MoveCall.arguments[2];
    const { bcs } = appRequire("@mysten/sui/bcs");
    assert.deepEqual(bcs.vector(bcs.Address).parse(Buffer.from(submitted.inputs[candidateArg.Input].Pure.bytes, "base64")), [members[2]]);
    await page.getByLabel("Skip never-admitted offline IDs (optional)").fill(members[3]);
    await page.getByLabel("Audit reason").fill("skip offline member before admission");
    await page.getByRole("button", { name: "Resume / apply skips" }).click();
    await page.waitForFunction(() => window.rolloutTransaction?.commands[0]?.MoveCall.function === "control_rollout");
    const recovery = await page.evaluate(() => window.rolloutTransaction);
    const skipArg = recovery.commands[0].MoveCall.arguments[2];
    assert.deepEqual(bcs.vector(bcs.Address).parse(Buffer.from(recovery.inputs[skipArg.Input].Pure.bytes, "base64")), [members[3]]);
    assert.equal(await page.evaluate(() => sessionStorage.getItem("suifleet.telemetryToken")), null);
    await page.close();

    // Grant and decision times beyond the Date range render raw instead of crashing the page.
    page = await open("/rollouts#far-future");
    await page.getByRole("button", { name: "app 2.0.0" }).click();
    await waitText(page, "#3 · UTC milliseconds 18446744073709551615 (out of range)");
    await waitText(page, "granted UTC milliseconds 18446744073709551615 (out of range)");
    await page.close();

    // Logs page: an account that is neither the budget owner nor a granted support account gets no archive controls.
    page = await open(`/data?device=${d1}`);
    await waitText(page, "not authorized to decrypt fleet archives");
    assert.equal(await page.getByRole("button", { name: "Request diagnostics" }).isDisabled(), true);
    assert.equal(await page.getByTestId("access-record").count(), 0);
    await page.close();

    // Search over a verified archive and the session access record; closing discards the plaintext.
    page = await open("/archive");
    await page.getByLabel("Search").fill("error");
    await page.waitForFunction(() => document.querySelector('[data-testid="search-count"]')?.textContent === "2 of 4 records match.");
    await page.getByLabel("Match case").check();
    await page.waitForFunction(() => document.querySelector('[data-testid="search-count"]')?.textContent === "1 of 4 records match.");
    await page.getByLabel("Match case").uncheck();
    await page.getByLabel("Search").fill("");
    await page.getByLabel("Source").selectOption("daemon");
    await page.waitForFunction(() => document.querySelector('[data-testid="search-count"]')?.textContent === "2 of 4 records match.");
    assert.ok((await text(page, "archive-view")).includes("4\t[daemon] shutdown"));
    assert.ok((await text(page, "access-record")).includes(`opened · support ${operator} · device ${d1} archive #3 (diagnostics)`));
    assert.equal(await page.evaluate(() => sessionStorage.length), 0);
    const archivePersisted = await page.evaluate(() => Object.values(localStorage).join("\n"));
    for (const secret of ["Shadow state snapshot", '"speed":1', '"mode":"eco"']) assert.ok(!archivePersisted.includes(secret), "no decrypted archive persisted; public event history may be cached");
    assert.ok(await page.evaluate(() => Object.keys(localStorage).every(k => k.startsWith("suifleet-history-v1:"))), "only public chain discovery may persist");
    await page.getByRole("button", { name: "Close and discard" }).click();
    await waitText(page, "Archive closed.");
    assert.ok(!(await page.textContent("body")).includes("ERROR disk full"));
    await page.close();

    // Decrypted shadow documents: the real archive verifier runs in the page;
    // values render as text, a rounded integer is never shown, history is labeled.
    page = await open("/shadows");
    await waitText(page, "Classic shadow · version 4");
    const docs = await text(page, "shadow-documents");
    for (const expected of ["Current: the device's newest archive (#4, sequence 9)", '"speed": 2', '"speed": 1', "Shadow config · version 7", "<b>3.1</b>",
      "Shadow big", "integers beyond the exact range", "1 more shadows did not fit"]) assert.ok(docs.includes(expected), `shadow documents lack ${expected}: ${docs}`);
    assert.ok(!docs.includes("18446744073709552000"), "a rounded integer must not be shown");
    assert.equal(await page.locator('[data-testid="shadow-documents"] b').count(), 0, "document strings render as text, never markup");
    assert.equal(await page.evaluate(() => sessionStorage.length), 0);
    const shadowPersisted = await page.evaluate(() => Object.values(localStorage).join("\n"));
    for (const secret of ["Shadow state snapshot", '"speed":1', '"mode":"eco"']) assert.ok(!shadowPersisted.includes(secret), "no decrypted archive persisted; public event history may be cached");
    assert.ok(await page.evaluate(() => Object.keys(localStorage).every(k => k.startsWith("suifleet-history-v1:"))), "only public chain discovery may persist");
    await page.close();
    page = await open("/shadows#history");
    await waitText(page, "History: archive #2 (sequence 9)");
    await page.close();
    page = await open("/shadows#tampered");
    await waitText(page, "Archive unavailable: Archive signature mismatch");
    assert.ok(!(await page.textContent("body")).includes("Classic shadow"));
    await page.close();
    assert.deepEqual(errors, []);
    console.log("PASS: real browser SF-084 dashboard: verified shadows/streams/messaging/bridge views with stream sequence ranges and export checkpoints, "
      + "signed encrypted-archive shadow documents (current/history/imprecise/tampered, text-only), alert drill-down, forged report hidden and alerts unavailable, aborted rollout overview/stages/audit (out-of-range times shown raw), active on-chain advance without indexer token or daily health reads, archive role gating, literal search, access record and discard");
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise((resolve) => server.close(resolve));
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
