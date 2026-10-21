// SF-100 bulk enrollment on a disposable loopback localnet (start it first):
//
//  1. Chain limits and authority, through the dApp's own builders: 260
//     reservations registered and funded in two PTBs (250 + resumed 10), 101
//     devices enrolling themselves (device key sends, claim key sponsors),
//     bound in two PTBs (100 + resumed 1). Replayed, revoked, expired,
//     unsponsored and cross-fleet claims are refused on chain; a device whose
//     owner cap left the operator is never bound. Gas and sizes are measured.
//  2. The real Enrollment and Devices pages in Chromium with a scripted
//     wallet: CSV + naming template + conflicts, guided single-device pairing,
//     downloaded credential archive, wallet rejection and resume, the real
//     daemon enrolling from the archive (when built), binding names/serials
//     and groups, reload resume, revocation, separate device gas funding and
//     address-free device listings. No key reaches storage, logs or URLs.
//
//   FLEET_TEST_RPC=http://127.0.0.1:9000 FLEET_TEST_FAUCET=http://127.0.0.1:9123 \
//   CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/test_bulk_enrollment_localnet.cjs [--evidence FILE]
const fs = require("node:fs/promises"), fsSync = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict"), { execFileSync } = require("node:child_process");
const { appRequire, loader } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions"), { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { fromBase58, fromHex, toHex, normalizeStructTag, normalizeSuiAddress } = appRequire("@mysten/sui/utils"), { bcs } = appRequire("@mysten/sui/bcs");
const { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet"), { decodeSuiPrivateKey } = appRequire("@mysten/sui/cryptography");
const rpc = process.env.FLEET_TEST_RPC || "http://127.0.0.1:9000", faucet = process.env.FLEET_TEST_FAUCET || "http://127.0.0.1:9123";
for (const endpoint of [rpc, faucet]) assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(new URL(endpoint).hostname), "refusing a non-loopback network");
const DAEMON = path.resolve(__dirname, "../suifleet-edge/target/debug/suifleet");
const evidencePath = process.argv.includes("--evidence") ? process.argv[process.argv.indexOf("--evidence") + 1] : undefined;
let stage = "startup";

function untar(buffer) {
  const files = new Map();
  for (let offset = 0; offset + 512 <= buffer.length;) {
    const header = buffer.subarray(offset, offset + 512);
    if (header.every((b) => b === 0)) break;
    const text = (a, b) => header.subarray(a, b).toString("latin1").replace(/\0.*$/s, "");
    const size = parseInt(text(124, 136), 8);
    files.set(text(0, 100), { mode: parseInt(text(100, 108), 8), body: buffer.subarray(offset + 512, offset + 512 + size).toString() });
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}
const credentialsOf = (files) => [...files.keys()].filter((n) => n.endsWith("/claim.key")).map((n) => {
  const folder = n.slice(0, -"claim.key".length), toml = files.get(folder + "enroll.toml").body;
  return { folder, serial: folder.split("/")[1], seedHex: files.get(n).body.trim(), alias: /^name = "(.*)"$/m.exec(toml)[1], files };
});

(async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-bulk-enrollment-"));
  const evidence = { network: "disposable localnet", transactions: [], refusals: [], browser: {} };
  let pageServer, browser;
  try {
    const client = new SuiGrpcClient({ network: "localnet", baseUrl: rpc });
    const operator = Ed25519Keypair.generate(), stranger = Ed25519Keypair.generate();
    async function fund(address) {
      for (let attempt = 0; attempt < 30; attempt++) {
        if ((await client.listCoins({ owner: address })).objects.length) return;
        try { await requestSuiFromFaucetV2({ host: faucet, recipient: address }); return; }
        catch (error) { if (attempt === 29) throw error; await new Promise((r) => setTimeout(r, 1000)); }
      }
    }
    stage = "faucet"; for (const k of [operator, stranger]) await fund(k.toSuiAddress());
    for (let i = 0; i < 2; i++) await requestSuiFromFaucetV2({ host: faucet, recipient: operator.toSuiAddress() }); // headroom for 260 claim fundings

    stage = "compile";
    const contracts = path.join(temporary, "contracts");
    await fs.mkdir(contracts);
    await fs.cp(path.resolve(__dirname, "../contracts/sources"), path.join(contracts, "sources"), { recursive: true });
    for (const name of ["Move.toml", "Move.lock"]) await fs.copyFile(path.resolve(__dirname, "../contracts", name), path.join(contracts, name));
    const compileEnv = { ...process.env, SUI_CONFIG_DIR: path.join(temporary, "wallet") };
    await fs.mkdir(compileEnv.SUI_CONFIG_DIR);
    const keystore = path.join(compileEnv.SUI_CONFIG_DIR, "sui.keystore");
    await fs.writeFile(keystore, "[]\n");
    await fs.writeFile(path.join(compileEnv.SUI_CONFIG_DIR, "client.yaml"), `keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: ${rpc}\n  ws: null\n  basic_auth: null\nactive_env: testnet\nactive_address: null\n`);
    let compiledText;
    try { compiledText = execFileSync("sui", ["move", "build", "--path", contracts, "--build-env", "testnet", "--no-tree-shaking", "--dump-bytecode-as-base64"], { env: compileEnv, encoding: "utf8", maxBuffer: 20 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] }); }
    catch { throw new Error("Could not compile isolated contracts; CLI output suppressed"); }
    const compiled = JSON.parse(compiledText.slice(compiledText.indexOf("{")));

    async function checkpointed(digest) {
      await client.waitForTransaction({ digest });
      for (let attempt = 0; attempt < 100; attempt++) {
        const confirmed = await client.getTransaction({ digest });
        if (confirmed.Transaction?.checkpoint != null || confirmed.FailedTransaction?.checkpoint != null) return;
        await new Promise((r) => setTimeout(r, 100));
      }
      throw new Error("transaction did not become checkpointed for history reads");
    }
    async function execute(transaction, signer = operator, label) {
      const result = await client.signAndExecuteTransaction({ transaction, signer, include: { effects: true, objectTypes: true, bcs: true } });
      if (result.FailedTransaction) throw new Error(`${label ?? stage}: ${result.FailedTransaction.status.error?.message || "execution failed"}`);
      assert.equal(result.Transaction.status.success, true);
      await checkpointed(result.Transaction.digest);
      if (label) {
        const gas = result.Transaction.effects.gasUsed;
        evidence.transactions.push({ label, digest: result.Transaction.digest, bytes: result.Transaction.bcs?.length,
          net_gas_mist: (BigInt(gas.computationCost) + BigInt(gas.storageCost) - BigInt(gas.storageRebate)).toString() });
      }
      return result.Transaction;
    }

    stage = "publish";
    const publish = new Transaction(), [upgradeCap] = publish.publish(compiled);
    publish.transferObjects([upgradeCap], operator.toSuiAddress());
    const published = await execute(publish);
    const packageId = published.effects.changedObjects.find((o) => o.outputState === "PackageWrite").objectId;
    const types = published.objectTypes;
    const registryId = Object.keys(types).find((id) => types[id].endsWith("::device_registry::DeviceRegistry"));
    const capId = Object.keys(types).find((id) => types[id].includes("::") && normalizeStructTag(types[id]) === normalizeStructTag("0x2::package::UpgradeCap"));
    const { object: packageObject } = await client.getObject({ objectId: packageId, include: { objectBcs: true } });
    const raw = bcs.Object.parse(packageObject.objectBcs).data.Package;
    const { object: registry } = await client.getObject({ objectId: registryId });
    const info = await client.ledgerService.getServiceInfo({});
    const manifest = {
      schema_version: 1, protocol_version: 1, network: "localnet", chain_identifier: toHex(fromBase58(info.response.chainId).slice(0, 4)), rpc_url: rpc,
      call_package_id: packageId, upgrade_cap_id: capId, package_version: raw.version,
      type_origins: Object.fromEntries(raw.typeOriginTable.map((entry) => [`${entry.moduleName}::${entry.datatypeName}`, entry.package])),
      shared_objects: { registry: { id: registryId, initial_shared_version: registry.owner.Shared.initialSharedVersion } },
      supported_clients: { dapp: { min: "0.0.2", max_exclusive: "0.2.0" }, edge: { min: "0.1.0", max_exclusive: "0.3.0" } },
      walrus: { publisher_url: "http://127.0.0.1:31415", aggregator_url: "http://127.0.0.1:31416" },
    };
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
    const load = loader(), e = load("lib/enrollment.ts"), sui = load("lib/sui.ts"), protocol = load("lib/fleet-protocol.ts");
    const op = normalizeSuiAddress(operator.toSuiAddress());

    /** A device enrolling itself: its key sends, the claim key sponsors. */
    async function enroll(policyId, seedHex, name, device = Ed25519Keypair.generate(), sponsor = true) {
      const claim = Ed25519Keypair.fromSecretKey(fromHex(seedHex));
      const tx = new Transaction();
      tx.setSender(device.toSuiAddress());
      if (sponsor) tx.setGasOwner(claim.toSuiAddress());
      protocol.calls.enrollment_enroll(tx, packageId, policyId, registryId, name, "aarch64", "0x6");
      const bytes = await tx.build({ client });
      const signatures = [(await device.signTransaction(bytes)).signature];
      if (sponsor) signatures.push((await claim.signTransaction(bytes)).signature);
      const result = await client.executeTransaction({ transaction: bytes, signatures, include: { effects: true } });
      if (result.FailedTransaction) return { ok: false, error: result.FailedTransaction.status.error?.message ?? "failed" };
      await checkpointed(result.Transaction.digest);
      return { ok: true, digest: result.Transaction.digest, device };
    }
    async function refused(label, attempt) {
      let outcome;
      try { outcome = await attempt(); } catch (error) { outcome = { ok: false, error: error.message }; }
      assert.equal(outcome.ok, false, `${label} was accepted`);
      evidence.refusals.push({ label, error: String(outcome.error).slice(0, 160) });
    }

    // ── 1. Chain limits and authority through the dApp builders ─────────────
    stage = "policies";
    const bulkPolicy = await e.createdPolicy(client, (await execute(e.buildCreatePolicyTx(300, 0), operator, "create policy")).digest);
    const strangerPolicy = await e.createdPolicy(client, (await execute(e.buildCreatePolicyTx(5, 0), stranger)).digest);
    await execute(sui.buildCreateGroupTx("Lab", "bulk enrollment"));
    let groups = await sui.fetchGroups(client, op);
    const lab = groups.find((g) => g.name === "Lab");
    assert.ok((await e.fetchPolicies(client, op)).some((p) => p.id === bulkPolicy));
    assert.ok(!(await e.fetchPolicies(client, op)).some((p) => p.id === strangerPolicy), "another operator's policy was listed");

    stage = "reservations";
    const csv = "serial,alias,group\n" + Array.from({ length: 260 }, (_, i) => `B${String(i + 1).padStart(4, "0")},,Lab`).join("\n") + "\n";
    const roster = e.parseRoster(csv, "bulk-{n:4}");
    const check = e.checkReservations(roster.rows, { devices: [], groups, reserved: [] });
    assert.equal(check.accepted.length, 260);
    const generated = e.generateCredentials(check.accepted);
    const seeds = new Map(generated.credentials.map((c) => [c.claim, c.seedHex]));
    let job = { ...e.newJob(op, bulkPolicy), entries: generated.entries };
    const statuses = async () => { const chain = await e.snapshot(client, job); return { chain, views: e.entryStatuses(job, chain) }; };
    const count = (views, status) => views.filter((v) => v.status === status).length;

    stage = "register claims (interrupted after the first transaction)";
    let { chain, views } = await statuses();
    let steps = e.plan(job, views, chain);
    assert.deepEqual(steps.map((s) => [s.kind, s.entries.length]), [["claims", 250], ["claims", 10]]);
    await execute(steps[0].build(), operator, "register+fund 250 claims");
    ({ chain, views } = await statuses());
    assert.deepEqual([count(views, "awaiting-device"), count(views, "unregistered")], [250, 10]);
    steps = e.plan(job, views, chain);
    assert.deepEqual(steps.map((s) => [s.kind, s.entries.length]), [["claims", 10]], "resume did not re-plan only the remainder");
    await execute(steps[0].build(), operator, "register+fund 10 claims (resumed)");
    // As the page does: registered claims are marked, so one revoked later is never re-registered.
    job = { ...job, entries: job.entries.map((x) => ({ ...x, submitted: true })) };
    // Repeating a registration is harmless: duplicates are ignored on chain.
    await execute(e.buildClaimsTx(bulkPolicy, job.entries.slice(0, 5).map((x) => x.claim), []));
    ({ chain, views } = await statuses());
    assert.equal(count(views, "awaiting-device"), 260);
    assert.equal(chain.policy.unusedClaims, 260);
    assert.equal(chain.balances.get(job.entries[0].claim), e.DEFAULT_CLAIM_FUND_MIST);

    stage = "101 devices enroll themselves";
    const started = Date.now();
    const deviceKeys = new Map();
    for (const entry of job.entries.slice(0, 101)) {
      const result = await enroll(bulkPolicy, seeds.get(entry.claim), entry.alias);
      assert.ok(result.ok, `${entry.alias}: ${result.error}`);
      deviceKeys.set(entry.claim, result.device);
    }
    evidence.enroll_101_devices_ms = Date.now() - started;

    stage = "refusals";
    const used = job.entries[0], revoked = job.entries[101], expired = job.entries[102];
    await refused("replayed claim", () => enroll(bulkPolicy, seeds.get(used.claim), "replay"));
    await refused("unsponsored enrollment", async () => { const d = Ed25519Keypair.generate(); await fund(d.toSuiAddress()); return enroll(bulkPolicy, seeds.get(job.entries[103].claim), "self-paid", d, false); });
    await execute(e.buildRevokeClaimsTx(bulkPolicy, [revoked.claim]), operator, "revoke claim");
    await refused("revoked claim", () => enroll(bulkPolicy, seeds.get(revoked.claim), "revoked"));
    await execute(e.buildSetLimitsTx(bulkPolicy, 300, 1));
    await refused("expired policy", () => enroll(bulkPolicy, seeds.get(expired.claim), "expired"));
    await execute(e.buildSetLimitsTx(bulkPolicy, 300, 0));
    await refused("claim of this fleet under another operator's policy", () => enroll(strangerPolicy, seeds.get(job.entries[104].claim), "cross-fleet"));
    const strangerClaim = e.generateCredentials([{ serial: "X1", alias: "x-1" }]).credentials[0];
    await execute(e.buildClaimsTx(strangerPolicy, [strangerClaim.claim], [{ claim: strangerClaim.claim, mist: e.DEFAULT_CLAIM_FUND_MIST }]), stranger);
    await refused("another operator's claim under this policy", () => enroll(bulkPolicy, strangerClaim.seedHex, "foreign"));
    assert.ok((await enroll(strangerPolicy, strangerClaim.seedHex, "bulk-0002")).ok, "the stranger's own claim should work in their policy");
    await refused("non-operator registering claims", () => execute(e.buildClaimsTx(bulkPolicy, [strangerClaim.claim], []), stranger).then(() => ({ ok: true })));
    ({ chain, views } = await statuses());
    assert.equal(count(views, "awaiting-device") + count(views, "unfunded"), 158, "a refused attempt consumed a claim"); // 260 - 101 - 1 revoked
    assert.equal(views.find((v) => v.entry.claim === revoked.claim).status, "withdrawn");
    assert.ok(!e.plan(job, views, chain).some((s) => s.entries.some((x) => x.claim === revoked.claim)), "a revoked claim was planned for re-registration");
    assert.equal(chain.enrollments.size, 101, "another policy's enrollment was attributed to this job");

    stage = "bind 101 devices (interrupted after the first transaction)";
    assert.equal(count(views, "binding"), 101);
    steps = e.plan(job, views, chain).filter((s) => s.kind === "bind");
    assert.deepEqual(steps.map((s) => s.entries.length), [100, 1]);
    await execute(steps[0].build(), operator, "bind 100 devices (alias, serial, group)");
    ({ chain, views } = await statuses());
    assert.deepEqual([count(views, "complete"), count(views, "binding")], [100, 1]);
    steps = e.plan(job, views, chain).filter((s) => s.kind === "bind");
    assert.deepEqual(steps.map((s) => s.entries.length), [1]);
    await execute(steps[0].build(), operator, "bind 1 device (resumed)");
    ({ chain, views } = await statuses());
    assert.equal(count(views, "complete"), 101);
    assert.equal(e.plan(job, views, chain).filter((s) => s.kind === "bind").length, 0, "a completed bind was planned again");
    const fleet = await sui.fetchDevices(client, op);
    const bound = fleet.find((d) => d.objectId === chain.enrollments.get(job.entries[5].claim).deviceId);
    assert.deepEqual([bound.name, bound.serial, bound.deviceAddress], [job.entries[5].alias, job.entries[5].serial, normalizeSuiAddress(deviceKeys.get(job.entries[5].claim).toSuiAddress())]);
    assert.equal((await sui.fetchGroups(client, op)).find((g) => g.objectId === lab.objectId).deviceIds.length, 101);

    stage = "owner cap transferred away";
    const moved = views.find((v) => v.entry.claim === job.entries[7].claim);
    const transfer = new Transaction(); transfer.transferObjects([moved.device.ownerCapId], stranger.toSuiAddress());
    await execute(transfer);
    ({ chain, views } = await statuses());
    assert.equal(views.find((v) => v.entry.claim === job.entries[7].claim).status, "untrusted");
    evidence.chain_scale = { reservations: 260, enrolled: 101, claims_per_tx: e.LIMITS.claimsPerTx, binds_per_tx: e.LIMITS.bindsPerTx };

    // ── 2. The real pages in a browser ───────────────────────────────────────
    stage = "browser bundle";
    const bundle = path.join(temporary, "app.js");
    await appRequire("esbuild").build({ entryPoints: [path.resolve(__dirname, "browser/enrollment-acceptance.jsx")], bundle: true, platform: "browser", outfile: bundle,
      jsx: "automatic", nodePaths: [path.resolve(__dirname, "../dapp/node_modules")], alias: { "@": path.resolve(__dirname, "../dapp/src") },
      define: { "process.env": "{}", "process.env.NODE_ENV": '"development"', "process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST": JSON.stringify(JSON.stringify(manifest)),
        "process.env.NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL": "undefined", "process.env.NEXT_PUBLIC_WALRUS_EPOCHS": "undefined", "process.env.NEXT_PUBLIC_WALRUS_MAX_COST_FROST": "undefined",
        LIVE_MANIFEST: JSON.stringify(manifest), OPERATOR_SEED: JSON.stringify(toHex(decodeSuiPrivateKey(operator.getSecretKey()).secretKey)) }, logLevel: "warning" });
    // gRPC-Web goes through the page's own origin to the loopback node.
    pageServer = http.createServer(async (req, res) => {
      if (req.method === "GET" && (req.url === "/" || req.url === "/app.js")) {
        res.setHeader("Content-Type", req.url === "/app.js" ? "text/javascript" : "text/html");
        return res.end(req.url === "/app.js" ? await fs.readFile(bundle) : '<!doctype html><html><head><title>Enrollment acceptance</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>');
      }
      const upstream = http.request(new URL(req.url, rpc), { method: req.method, headers: { ...req.headers, host: new URL(rpc).host } }, (up) => { res.writeHead(up.statusCode, up.headers); up.pipe(res); });
      upstream.on("error", () => { res.statusCode = 502; res.end(); });
      req.pipe(upstream);
    });
    await new Promise((resolve) => pageServer.listen(0, "127.0.0.1", resolve));
    const origin = `http://127.0.0.1:${pageServer.address().port}`;
    browser = await appRequire("playwright").chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
    const context = await browser.newContext({ acceptDownloads: true });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [], logs = [], alerts = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => logs.push(message.text()));
    page.on("dialog", (dialog) => { if (dialog.type() === "alert") alerts.push(dialog.message()); dialog.accept(); });
    const counts = () => page.getByTestId("status-counts").innerText();
    const waitCounts = async (expected) => {
      for (let i = 0; i < 120; i++) { const text = await counts().catch(() => ""); if (expected.every((x) => text.includes(x))) return text; await page.waitForTimeout(500); }
      throw new Error(`status counts never showed ${expected.join(", ")}: ${await counts()}`);
    };
    const downloadFrom = async (button) => {
      const [download] = await Promise.all([page.waitForEvent("download"), button.click()]);
      return fs.readFile(await download.path());
    };
    async function connect() {
      await page.goto(origin);
      await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
      await page.getByRole("button", { name: "SuiFleet Enrollment Test Wallet", exact: false }).click();
    }

    stage = "browser: policy";
    await connect();
    await page.getByLabel(/^Quota/).fill("10");
    await page.getByRole("button", { name: "Create policy" }).click();
    await page.getByText("Accepting enrollments.").waitFor();
    const uiPolicy = await page.locator("#policy").inputValue();
    assert.match(uiPolicy, /^0x[0-9a-f]{64}$/);
    assert.equal(normalizeSuiAddress((await e.readPolicy(client, uiPolicy)).operator), op);

    stage = "browser: CSV with template and conflicts";
    await page.getByLabel("Roster CSV", { exact: true }).fill("serial,alias,group\nSN-001,,Lab\nSN-002,,Lab\nSN-003,kitchen,\nSN-001,,\nSN-004,bad name,\nSN-005,bulk-0003,\n");
    await page.getByLabel("Naming template").fill("pi-{n:3}");
    await page.getByRole("button", { name: "Check", exact: true }).click();
    const checkText = await page.getByTestId("reservation-check").innerText();
    assert.match(checkText, /3 ready, 3 rejected/);
    assert.match(checkText, /serial SN-001 is already used/);
    assert.match(checkText, /alias bulk-0003 is already used by device bulk-0003/);
    const csvFiles = untar(await downloadFrom(page.getByRole("button", { name: "Download credentials for 3 devices" })));
    const csvCreds = credentialsOf(csvFiles);
    assert.deepEqual(csvCreds.map((c) => [c.serial, c.alias]), [["SN-001", "pi-001"], ["SN-002", "pi-002"], ["SN-003", "kitchen"]]);
    assert.ok(csvCreds.every((c) => csvFiles.get(c.folder + "claim.key").mode === 0o600));
    assert.equal(csvFiles.get(csvCreds[0].folder + "enroll.toml").body.includes(`policy_id = "${uiPolicy}"`), true);

    stage = "browser: guided pairing";
    await page.getByRole("tab", { name: "Pair one device" }).click();
    await page.getByLabel("Alias").fill("pi-solo");
    await page.getByLabel("Serial number").fill("SN-100");
    await page.getByLabel("Default group").selectOption({ label: "Lab" });
    await page.getByRole("button", { name: "Check", exact: true }).click();
    const soloCreds = credentialsOf(untar(await downloadFrom(page.getByRole("button", { name: "Download credentials for 1 device" }))));
    assert.deepEqual(soloCreds.map((c) => [c.serial, c.alias]), [["SN-100", "pi-solo"]]);
    const allSeeds = [...csvCreds, ...soloCreds].map((c) => c.seedHex);
    await waitCounts(["Reserved: 4"]);

    stage = "browser: rejected wallet then resume";
    const callsBefore = await page.evaluate(() => window.enrollment.calls()); // policy creation
    await page.evaluate(() => window.enrollment.reject(true));
    await page.getByRole("button", { name: "Run 1 pending transaction" }).click();
    await page.getByRole("alert").filter({ hasText: "User rejected" }).waitFor();
    await waitCounts(["Reserved: 4"]);
    assert.equal(await page.evaluate(() => window.enrollment.calls()), callsBefore + 1);
    await page.evaluate(() => window.enrollment.reject(false));
    await page.getByRole("button", { name: "Run 1 pending transaction" }).click();
    await waitCounts(["Waiting for device: 4"]);
    assert.equal(await page.evaluate(() => window.enrollment.calls()), callsBefore + 2, "one approval for all four claims");

    stage = "devices enroll themselves from the downloaded archive";
    const byAlias = Object.fromEntries([...csvCreds, ...soloCreds].map((c) => [c.alias, c]));
    assert.ok((await enroll(uiPolicy, byAlias["pi-001"].seedHex, "pi-001")).ok);
    // The device-chosen name is not authoritative: the operator's alias wins.
    assert.ok((await enroll(uiPolicy, byAlias["pi-002"].seedHex, "device-chosen")).ok);
    if (fsSync.existsSync(DAEMON)) {
      const media = path.join(temporary, "media-kitchen");
      await fs.mkdir(media, { mode: 0o700 });
      for (const name of ["enroll.toml", "claim.key", "deployment.json"]) {
        let body = byAlias.kitchen.files.get(byAlias.kitchen.folder + name).body;
        // The device's own key location; everything else is the archive's.
        if (name === "enroll.toml") body = body.replace('keypair_path = "/etc/suifleet/device.key"', `keypair_path = "${path.join(media, "device.key")}"`);
        await fs.writeFile(path.join(media, name), body, { mode: 0o600 });
      }
      execFileSync(DAEMON, ["--provision", path.join(media, "device.key")], { stdio: ["ignore", "pipe", "pipe"] });
      const output = JSON.parse(execFileSync(DAEMON, ["--enroll", path.join(media, "enroll.toml")], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }));
      await checkpointed(output.digest);
      evidence.browser.daemon_enrollment = { digest: output.digest, device_object_id: output.device_object_id };
    } else {
      assert.ok((await enroll(uiPolicy, byAlias.kitchen.seedHex, "kitchen")).ok);
      evidence.browser.daemon_enrollment = "skipped: suifleet-edge/target/debug/suifleet not built";
    }
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await waitCounts(["Enrolled, naming: 3", "Waiting for device: 1"]);

    stage = "browser: interrupted bind then resume";
    await page.evaluate(() => window.enrollment.reject(true));
    await page.getByRole("button", { name: "Run 1 pending transaction" }).click();
    await page.getByRole("alert").filter({ hasText: "Name and group devices 1/1: User rejected" }).waitFor();
    await waitCounts(["Enrolled, naming: 3"]);
    await page.evaluate(() => window.enrollment.reject(false));
    await page.getByRole("button", { name: "Run 1 pending transaction" }).click();
    await waitCounts(["Enrolled: 3", "Waiting for device: 1"]);
    const named = Object.fromEntries((await sui.fetchDevices(client, op)).map((d) => [d.name, d]));
    assert.deepEqual(["pi-001", "pi-002", "kitchen"].map((n) => named[n]?.serial), ["SN-001", "SN-002", "SN-003"]);
    assert.equal(named["pi-002"].chainName, "device-chosen");
    const labMembers = (await sui.fetchGroups(client, op)).find((g) => g.objectId === lab.objectId).deviceIds;
    assert.ok(labMembers.includes(named["pi-001"].objectId) && labMembers.includes(named["pi-002"].objectId) && !labMembers.includes(named.kitchen.objectId));

    stage = "browser: reload resumes from the stored job";
    await connect();
    await page.locator("#policy").selectOption(uiPolicy);
    await waitCounts(["Enrolled: 3", "Waiting for device: 1"]);
    const stored = await page.evaluate(() => Object.keys(localStorage).map((k) => localStorage.getItem(k)).join("\n"));
    for (const seed of allSeeds) {
      assert.ok(!stored.includes(seed), "a claim key reached localStorage");
      assert.ok(!logs.some((l) => l.includes(seed)), "a claim key reached the console");
      assert.ok(!page.url().includes(seed));
    }

    stage = "browser: revoke unused, fund device gas separately";
    await page.getByRole("button", { name: "Revoke unused claims" }).click();
    await waitCounts(["Revoked: 1", "Enrolled: 3"]);
    await refused("revoked pairing credential", () => enroll(uiPolicy, byAlias["pi-solo"].seedHex, "pi-solo"));
    const deviceAddresses = ["pi-001", "pi-002", "kitchen"].map((n) => named[n].deviceAddress);
    const before = await e.balances(client, deviceAddresses);
    await page.getByLabel("SUI per device").fill("0.01");
    await page.getByRole("button", { name: "Fund enrolled devices" }).click();
    for (let i = 0; i < 60; i++) { const after = await e.balances(client, deviceAddresses); if (deviceAddresses.every((a) => after.get(a) === before.get(a) + BigInt(10_000_000))) break; if (i === 59) throw new Error("device gas not funded"); await page.waitForTimeout(500); }

    stage = "browser: export job";
    const exported = (await downloadFrom(page.getByRole("button", { name: "Export job" }))).toString();
    assert.equal(e.parseJob(exported, { operator: op, policyId: uiPolicy }).entries.length, 4);
    assert.ok(allSeeds.every((s) => !exported.includes(s)));

    stage = "browser: address-free device listing";
    await page.getByRole("button", { name: "Devices page" }).click();
    const row = page.locator("tr", { has: page.getByText("pi-001", { exact: true }) });
    await row.waitFor();
    assert.ok((await row.innerText()).includes("SN-001"));
    assert.ok(!(await page.locator("table").first().innerText()).includes(named["pi-001"].deviceAddress.slice(0, 10)), "an address is shown by default");
    await page.getByLabel("Technical details").check();
    await page.getByText(named["pi-001"].deviceAddress).waitFor();
    assert.deepEqual(errors, []);
    evidence.browser.wallet_calls = await page.evaluate(() => window.enrollment.calls());
    evidence.browser.alerts = alerts;
    if (evidencePath) await fs.writeFile(evidencePath, JSON.stringify(evidence, null, 2) + "\n");
    console.log(JSON.stringify(evidence.transactions, null, 1));
    console.log(`PASS: localnet bulk enrollment: 260 reservations in 2 claim PTBs (resumed), 101 self-enrolled devices bound in 2 PTBs (resumed) in ${evidence.enroll_101_devices_ms} ms of enrollment; `
      + `${evidence.refusals.length} refusals (${evidence.refusals.map((r) => r.label).join("; ")}); foreign owner cap not bound. Browser: CSV/template/conflicts, `
      + `guided pairing, archive credentials, ${typeof evidence.browser.daemon_enrollment === "string" ? "" : "real daemon --enroll, "}rejected-wallet resume, bind, reload resume, revocation, device gas, address-free listing; no key in storage/console/URL`);
  } catch (error) {
    console.error(`FAILED at ${stage}`);
    throw error;
  } finally {
    if (browser) await browser.close();
    if (pageServer) await new Promise((resolve) => pageServer.close(resolve));
    await fs.rm(temporary, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
