// Live browser acceptance runner (SF-010); driven by scripts/live_wallet_gate.py.
// Env: LIVE_MANIFEST_FILE, LIVE_SEED (throwaway funded test key, hex),
// LIVE_DEVICE_ADDRESS, LIVE_RETURN_ADDRESS, LIVE_EVIDENCE.
// Prints one PASS line per check; writes digests and blob IDs (never keys).
const fs = require("node:fs/promises"), fsSync = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { build } = appRequire("esbuild"), { chromium } = appRequire("playwright");
const component = (version, run) => `#!/bin/sh\n# suifleet live browser acceptance ${version} ${run}\nexit 0\n`;
(async () => {
  const evidence = { checks: [], transactions: [], blobs: [] };
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-live-browser-"));
  let server, browser;
  try {
    const manifest = JSON.parse(fsSync.readFileSync(process.env.LIVE_MANIFEST_FILE));
    const bundle = path.join(dir, "app.js");
    await build({ entryPoints: [path.resolve(__dirname, "browser/live-acceptance.jsx")], bundle: true, platform: "browser", outfile: bundle,
      jsx: "automatic", nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") },
      define: { "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(manifest)),
        "process.env.NEXT_PUBLIC_WALRUS_EPOCHS": '"1"', "process.env.NEXT_PUBLIC_WALRUS_MAX_COST_FROST": "undefined", LIVE_MANIFEST: JSON.stringify(manifest), LIVE_SEED: JSON.stringify(process.env.LIVE_SEED) },
      logLevel: "warning" });
    server = http.createServer(async (req, res) => {
      res.setHeader("Content-Type", req.url === "/app.js" ? "text/javascript" : "text/html");
      res.end(req.url === "/app.js" ? await fs.readFile(bundle) : '<!doctype html><html><head><title>Live acceptance</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>');
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const page = await browser.newPage();
    const errors = [], logs = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => logs.push(`${message.type()}: ${message.text()}`));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    try {
      await page.waitForFunction(() => window.live?.ready && window.live.wallets().includes("SuiFleet Live Test Wallet"), null, { timeout: 60000 });
    } catch (error) {
      console.error("page did not become ready", JSON.stringify({ errors, logs: logs.slice(-20) }, null, 2));
      throw error;
    }
    const pass = (label) => { evidence.checks.push(label); console.log("PASS:", label); };
    const live = (fn, ...args) => page.evaluate(fn, ...args);
    const submit = async (label, builder, ...args) => {
      const result = await live(([b, a]) => window.live.submit(b, ...a), [builder, args]);
      evidence.transactions.push({ label, digest: result.digest, status: "success" });
      return result.digest;
    };
    const fails = async (label, fn, arg, pattern) => {
      const outcome = await live(async ([source, a]) => { try { await eval(source)(a); return "no error"; } catch (error) { return String(error.message ?? error); } }, [fn.toString(), arg]);
      assert.match(outcome, pattern, `${label}: ${outcome}`);
      evidence.transactions.push({ label, status: "refused", error: outcome.slice(0, 300) });
      return outcome;
    };

    // Connect through the real dApp Kit connect UI.
    await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
    await page.getByRole("button", { name: "SuiFleet Live Test Wallet", exact: false }).click();
    const address = await live(() => window.live.address);
    await page.waitForFunction((a) => document.querySelector("#account").textContent === a, address);
    pass(`connected through the dApp Kit connect UI as ${address.slice(0, 12)}… on ${manifest.network}`);

    // Rejection by the wallet surfaces as an error; nothing is submitted.
    await live(() => window.live.reject(true));
    await fails("wallet rejection", (a) => window.live.submit("buildCreateGroupTx", "rejected", "never"), null, /rejected/i);
    await live(() => window.live.reject(false));
    pass("wallet rejection surfaces as an error; nothing executed");

    // Fresh registration: register + cap + activation in one dApp transaction.
    const deviceAddress = process.env.LIVE_DEVICE_ADDRESS;
    await submit("register + issue cap + activate device", "buildRegisterDeviceTx", "live-browser-pi", deviceAddress, "aarch64", address);
    let devices = [];
    for (let i = 0; i < 30 && !devices.length; i++) { devices = await live(() => window.live.devices()); if (!devices.length) await new Promise((r) => setTimeout(r, 1000)); }
    const device = devices.find((d) => d.deviceAddress === deviceAddress);
    assert.ok(device && device.ownerCapId, "registered device not listed as activated");
    pass("fresh registration: device registered, cap issued and activated (owner cap held by the wallet), listed by fetchDevices");

    // Group and membership.
    await submit("create group", "buildCreateGroupTx", "live-browser", "SF-010 acceptance");
    let group;
    for (let i = 0; i < 30 && !group; i++) { group = (await live(() => window.live.groups())).find((g) => g.name === "live-browser"); if (!group) await new Promise((r) => setTimeout(r, 1000)); }
    assert.ok(group, "group not listed");
    await submit("add device to group", "buildAddDeviceToGroupTx", group.objectId, { objectId: device.objectId, ownerCapId: device.ownerCapId });
    pass("group created and the activated device added (borrowed from custody and returned)");

    // Real Walrus upload through the dApp's upload code, then a verified deployment.
    const run = Date.now().toString(36);
    const uploaded = await live(([v, s]) => window.live.upload("suifleet-live-browser", v, s), ["1.0.0", component("1.0.0", run)]);
    evidence.blobs.push({ artifact: uploaded.artifact.blobId, recipe: uploaded.recipe.blobId });
    await submit("create verified deployment (group + device)", "buildCreateDeploymentTx", "suifleet-live-browser", "1.0.0",
      uploaded.recipe.blobId, uploaded.artifact.sha256, uploaded.recipe.sha256, [group.objectId], [device.objectId]);
    let deployment;
    for (let i = 0; i < 30 && !deployment; i++) { deployment = (await live(() => window.live.deployments())).find((d) => d.recipeBlobId === uploaded.recipe.blobId); if (!deployment) await new Promise((r) => setTimeout(r, 1000)); }
    assert.ok(deployment, "deployment not listed");
    assert.deepEqual(deployment.resolvedDeviceIds, [device.objectId]);
    assert.equal(deployment.receiptsVerified, true);
    const storage = await live((blob) => window.live.storage(blob), uploaded.recipe.blobId);
    assert.ok(JSON.stringify(storage).includes("available"), JSON.stringify(storage));
    pass(`recipe and artifact uploaded to Walrus ${manifest.network} by the dApp; verified deployment listed with its resolved device; blobs probed available`);

    // A transaction that would abort is refused before signing (simulation),
    // and one that does execute and abort on chain is reported as a failure.
    const again = { group: group.objectId, device: { objectId: device.objectId, ownerCapId: device.ownerCapId } };
    await live(() => window.live.resetDigest());
    await fails("abort caught by simulation (device already in group)", (a) => window.live.submit("buildAddDeviceToGroupTx", a.group, a.device),
      again, /resolution failed|abort/i);
    assert.equal(await live(() => window.live.lastDigest()), null, "a simulated abort must not be executed");
    pass("a transaction that would abort is refused by simulation before signing; nothing executed");
    await fails("on-chain abort (device already in group)", (a) => window.live.submitAborting("buildAddDeviceToGroupTx", a.group, a.device),
      again, /abort|fail|not successful/i);
    const aborted = await live(() => window.live.lastDigest());
    assert.ok(aborted, "the aborting transaction was never executed");
    const status = await live((digest) => window.live.executed(digest), aborted);
    assert.equal(status.success, false, JSON.stringify(status));
    evidence.transactions.at(-1).digest = aborted;
    evidence.transactions.at(-1).chain_status = status;
    pass(`a transaction executed and aborted on chain (${aborted.slice(0, 10)}…) is reported as failed by the hook`);

    // Wrong network: the hook refuses before signing.
    await live(() => window.live.unsupportedChain());
    await fails("unsupported account chain", () => window.live.submit("buildCreateGroupTx", "x", "x"), null, /does not support sui:/);
    await live(() => window.live.restoreChain());
    pass("an account without the manifest's chain is refused before signing");

    // Cleanup (storage rebates) and sweep the test key back to the funder.
    await submit("delete deployment", "buildDeleteDeploymentTx", deployment.deploymentId);
    await submit("remove device from group", "buildRemoveDeviceFromGroupTx", group.objectId, device.objectId);
    await submit("delete group", "buildDeleteGroupTx", group.objectId);
    await submit("deregister device", "buildDeregisterDeviceTx", { objectId: device.objectId, ownerCapId: device.ownerCapId });
    evidence.sweep = await live((to) => window.live.sweep(to), process.env.LIVE_RETURN_ADDRESS);
    pass("cleanup through the dApp (deployment, membership, group, device) and test key swept back");
    assert.deepEqual(errors, []);
  } finally {
    if (process.env.LIVE_EVIDENCE) fsSync.writeFileSync(process.env.LIVE_EVIDENCE, JSON.stringify(evidence, null, 2) + "\n");
    await browser?.close(); server?.close();
    await fs.rm(dir, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
