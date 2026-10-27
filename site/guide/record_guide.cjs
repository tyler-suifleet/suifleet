// Guide screencast recorder (SF-172), run by site/guide/record_guide.py.
//
// Serves the real console static export (dapp/out, built for the verified
// Testnet manifest) on loopback and drives it in headless Chromium through
// the steps in site/guide/steps.json, capturing each step as screencast frames
// that site/guide/encode_guide.py turns into WebM.
//
// The wallet is a scripted Wallet Standard wallet ("SuiFleet Guide Wallet")
// standing in for Slush or an extension. Its throwaway key stays in this Node
// process: the page asks Node to sign through an exposed binding and never
// sees the key, so no secret can appear on screen.
//
// Env: GUIDE_MANIFEST_FILE, GUIDE_SEED_FILE (0600 hex seed), GUIDE_EXPORT,
// GUIDE_OUT (private frames/evidence dir), GUIDE_PHASE (sign-in | rest | rollout),
// GUIDE_STATE (browser storage carried between phases), GUIDE_DEVICES
// (JSON list of device addresses to register), GUIDE_EXTRA (rollout inputs
// read from chain), CHROME_EXECUTABLE.
const fs = require("node:fs"), fsp = require("node:fs/promises"), path = require("node:path"), http = require("node:http");
const { appRequire } = require("../../scripts/frontend_test_utils.cjs");
const { chromium } = appRequire("playwright");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { Transaction } = appRequire("@mysten/sui/transactions");
const { fromHex, toBase64 } = appRequire("@mysten/sui/utils");

const env = (name) => { const v = process.env[name]; if (!v) throw new Error(`${name} is required`); return v; };
const manifest = JSON.parse(fs.readFileSync(env("GUIDE_MANIFEST_FILE"), "utf8"));
const keypair = Ed25519Keypair.fromSecretKey(fromHex(fs.readFileSync(env("GUIDE_SEED_FILE"), "utf8").trim()));
const address = keypair.getPublicKey().toSuiAddress();
const chain = `sui:${manifest.network}`;
const client = new SuiGrpcClient({ network: manifest.network, baseUrl: manifest.rpc_url });
const out = env("GUIDE_OUT"), exportDir = path.resolve(env("GUIDE_EXPORT")), phase = env("GUIDE_PHASE");
const evidencePath = path.join(out, `evidence-${phase}.json`);
const evidence = { phase, wallet_address: address, transactions: [], steps: [] };
const VIEWPORT = { width: 1280, height: 800 };
let currentStep = null;

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".txt": "text/plain",
  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".wasm": "application/wasm", ".woff2": "font/woff2" };

function serve() {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    let file = path.join(exportDir, decodeURIComponent(url.pathname));
    if (!file.startsWith(exportDir)) { res.statusCode = 403; return res.end(); }
    try {
      if ((await fsp.stat(file)).isDirectory()) file = path.join(file, "index.html");
    } catch { file = path.join(exportDir, "404.html"); res.statusCode = 404; }
    try {
      res.setHeader("Content-Type", TYPES[path.extname(file)] || "application/octet-stream");
      res.end(await fsp.readFile(file));
    } catch { res.statusCode = 404; res.end(); }
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

// Runs in the page before any console code. Registers the wallet; every
// signature is requested from Node through window.__guideWallet.
const walletScript = ({ address, publicKey, chain }) => {
  const account = { address, publicKey: new Uint8Array(publicKey), chains: [chain], label: "Guide test account",
    features: ["sui:signTransaction", "sui:signAndExecuteTransaction", "sui:signPersonalMessage"] };
  const listeners = new Set();
  const icon = "data:image/svg+xml;base64," + btoa('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#1d4ed8"/><path d="M8 20.5 16 7l8 13.5-8 4.5z" fill="#fff"/></svg>');
  const wallet = {
    version: "1.0.0", name: "SuiFleet Guide Wallet", icon, chains: [chain], get accounts() { return [account]; },
    features: {
      "standard:connect": { version: "1.0.0", connect: async () => ({ accounts: [account] }) },
      "standard:disconnect": { version: "1.0.0", disconnect: async () => listeners.forEach((fn) => fn({ accounts: [] })) },
      "standard:events": { version: "1.0.0", on: (_event, fn) => { listeners.add(fn); return () => listeners.delete(fn); } },
      "sui:signTransaction": { version: "2.0.0", signTransaction: async ({ transaction, chain: c }) =>
        window.__guideWallet("sign", { tx: await transaction.toJSON(), chain: c }) },
      "sui:signAndExecuteTransaction": { version: "2.0.0", signAndExecuteTransaction: async ({ transaction, chain: c }) =>
        window.__guideWallet("execute", { tx: await transaction.toJSON(), chain: c }) },
      "sui:signPersonalMessage": { version: "1.1.0", signPersonalMessage: async ({ message }) =>
        window.__guideWallet("personal", { message: Array.from(message) }) },
    },
  };
  window.addEventListener("wallet-standard:app-ready", (event) => event.detail.register(wallet));
  window.dispatchEvent(new CustomEvent("wallet-standard:register-wallet", { detail: ({ register }) => register(wallet) }));
};

// Caption bar and click marker, drawn with the CSSOM (allowed by the console CSP).
const overlayScript = () => {
  const mount = () => {
    if (document.getElementById("guide-caption")) return;
    const bar = document.createElement("div");
    bar.id = "guide-caption";
    Object.assign(bar.style, { position: "fixed", left: "0", right: "0", bottom: "0", zIndex: "2147483647", padding: "10px 18px",
      background: "rgba(15,23,42,.88)", color: "#fff", font: "600 17px system-ui, sans-serif", pointerEvents: "none", display: "none" });
    document.documentElement.appendChild(bar);
  };
  window.__guideCaption = (text) => { mount(); const bar = document.getElementById("guide-caption"); bar.textContent = text; bar.style.display = text ? "block" : "none"; };
  window.__guideMark = (x, y) => {
    const ring = document.createElement("div");
    Object.assign(ring.style, { position: "fixed", left: `${x - 18}px`, top: `${y - 18}px`, width: "36px", height: "36px", borderRadius: "50%",
      border: "3px solid #f59e0b", background: "rgba(245,158,11,.25)", zIndex: "2147483647", pointerEvents: "none" });
    document.documentElement.appendChild(ring);
    setTimeout(() => ring.remove(), 700);
  };
};

async function walletBinding(_source, op, payload) {
  if (payload.chain && payload.chain !== chain) throw new Error(`refusing to sign for ${payload.chain}`);
  if (op === "personal") {
    const { bytes, signature } = await keypair.signPersonalMessage(Uint8Array.from(payload.message));
    return { bytes, signature };
  }
  const transaction = Transaction.from(payload.tx);
  transaction.setSenderIfNotSet(address);
  if (op === "sign") {
    const bytes = await transaction.build({ client });
    return keypair.signTransaction(bytes);
  }
  if (op === "execute") {
    const bytes = await transaction.build({ client });
    const result = await keypair.signAndExecuteTransaction({ transaction, client });
    const tx = result.Transaction ?? result.FailedTransaction;
    evidence.transactions.push({ step: currentStep, digest: tx.digest, success: !!result.Transaction });
    console.log(`tx ${currentStep}: ${tx.digest} ${result.Transaction ? "success" : "FAILED"}`);
    return { bytes: toBase64(bytes), signature: tx.signatures[0], digest: tx.digest, effects: toBase64(tx.effects.bcs) };
  }
  throw new Error(`unsupported wallet operation ${op}`);
}

class Recorder {
  constructor(page, cdp) { this.page = page; this.cdp = cdp; this.frames = null; this.dir = null; this.count = 0;
    cdp.on("Page.screencastFrame", ({ data, metadata, sessionId }) => {
      this.cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
      if (!this.frames) return;
      const name = `${String(++this.count).padStart(5, "0")}.jpg`;
      fs.writeFileSync(path.join(this.dir, name), Buffer.from(data, "base64"));
      this.frames.push({ file: name, t: metadata.timestamp });
    });
  }
  async start(step) {
    this.dir = path.join(out, "frames", step);
    fs.rmSync(this.dir, { recursive: true, force: true });
    fs.mkdirSync(this.dir, { recursive: true });
    this.frames = []; this.count = 0;
    await this.cdp.send("Page.startScreencast", { format: "jpeg", quality: 92, maxWidth: VIEWPORT.width, maxHeight: VIEWPORT.height });
  }
  async stop() {
    await this.page.waitForTimeout(1500);
    await this.cdp.send("Page.stopScreencast");
    const frames = this.frames; this.frames = null;
    fs.writeFileSync(path.join(this.dir, "frames.json"), JSON.stringify(frames));
    await this.page.screenshot({ path: path.join(this.dir, "poster.png") });
    return frames.length;
  }
}

function helpers(page) {
  const caption = (text) => page.evaluate((t) => window.__guideCaption?.(t), text);
  const pause = (ms = 900) => page.waitForTimeout(ms);
  async function click(locator) {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    if (box) await page.evaluate(([x, y]) => window.__guideMark?.(x, y), [box.x + box.width / 2, box.y + box.height / 2]);
    await pause(450);
    await locator.click();
  }
  async function type(locator, text, delay = 35) {
    await click(locator);
    await locator.fill("");
    await locator.pressSequentially(text, { delay });
  }
  const sidebar = (label) => page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: label, exact: true }).first();
  return { caption, pause, click, type, sidebar };
}

// Wait until the wallet has executed `count` more transactions in this step.
async function transactions(page, before, count, timeout = 120000) {
  const start = Date.now();
  while (evidence.transactions.length < before + count) {
    if (Date.now() - start > timeout) throw new Error(`${currentStep}: expected ${count} transaction(s), saw ${evidence.transactions.length - before}`);
    await page.waitForTimeout(500);
  }
  const failed = evidence.transactions.slice(before).filter((t) => !t.success);
  if (failed.length) throw new Error(`${currentStep}: transaction failed on chain: ${failed.map((t) => t.digest).join(", ")}`);
}

const STEPS = {
  async "sign-in"(page, h) {
    await page.goto(`${base}/get-started/`);
    await page.getByRole("heading", { name: "Get started with SuiFleet" }).waitFor();
    await h.caption("Open the console and choose Get started.");
    await h.pause(1500);
    await h.caption("Click Connect and choose your wallet: Slush, an extension, or Google where enabled.");
    await h.click(page.getByRole("button", { name: "Connect Wallet", exact: true }).first());
    await h.pause(1200);
    await h.caption("This recording uses a scripted test wallet in place of Slush; approve the connection.");
    await h.click(page.getByRole("button", { name: /SuiFleet Guide Wallet/ }).first());
    await page.locator("#step-account").getByText(/Network:/).waitFor({ timeout: 30000 });
    await h.caption("Connected: the card shows your address and the network (Testnet).");
    await h.pause(2000);
    const before = evidence.transactions.length;
    const create = page.getByRole("button", { name: "Create default fleet" });
    await create.waitFor({ timeout: 30000 });
    await h.caption("First time only: create your default fleet, then approve the transaction.");
    await h.click(create);
    await transactions(page, before, 1);
    await page.locator("#fleet-switcher").waitFor();
    await page.waitForFunction(() => document.querySelector("#fleet-switcher")?.value, null, { timeout: 60000 });
    await h.caption("Your fleet is selected in the Fleet switcher. Next: fund its network budget.");
    await h.pause(2500);
  },

  async fund(page, h) {
    await h.caption("Choose Network budget in the sidebar.");
    await h.click(h.sidebar("Network budget"));
    await page.getByRole("heading", { name: "Network budget", exact: true }).waitFor();
    await h.pause(1500);
    const amount = page.getByLabel(/^(Initial deposit|Deposit) \(SUI\)/);
    await h.caption("Enter a deposit in SUI. Devices draw report and storage fees from this budget, within your limits.");
    await h.type(amount, "0.02");
    const before = evidence.transactions.length;
    await h.caption("Create the budget and deposit, then approve the transaction.");
    await h.click(page.getByRole("button", { name: /^(Create budget and deposit|Deposit)$/ }).first());
    await transactions(page, before, 1);
    await page.getByRole("status").filter({ hasText: /Confirmed/ }).first().waitFor({ timeout: 60000 });
    await h.caption("Confirmed. The balance under Keep devices reporting now covers device activity.");
    await h.pause(3000);
  },

  async "register-device"(page, h) {
    await h.caption("Choose Devices in the sidebar.");
    await h.click(h.sidebar("Devices"));
    await page.getByRole("heading", { name: "Devices", exact: true }).waitFor();
    await h.pause(1200);
    await h.caption("Click Register by address. The address comes from the device: suifleet --derive-address.");
    await h.click(page.getByRole("button", { name: "Register by address" }));
    await h.type(page.locator('input[name="name"]'), "guide-pi-001");
    await h.type(page.locator('input[name="deviceAddress"]'), devices[0], 8);
    await h.click(page.locator('select[name="arch"]'));
    await page.locator('select[name="arch"]').selectOption("aarch64");
    const before = evidence.transactions.length;
    await h.caption("Click Register and approve the transaction.");
    await h.click(page.getByRole("button", { name: "Register", exact: true }));
    await transactions(page, before, 1);
    await page.getByText("guide-pi-001").first().waitFor({ timeout: 90000 });
    await h.caption("The device is registered. It reports once its client starts on the hardware.");
    await h.pause(3000);
  },

  async "enroll-batch"(page, h) {
    await h.caption("For a production batch, choose Enrollment in the sidebar.");
    await h.click(h.sidebar("Enrollment"));
    await page.getByRole("heading", { name: "Enrollment", exact: true }).waitFor();
    await h.pause(1200);
    await h.caption("1. Create a batch policy: how many devices may enroll, and for how long.");
    await h.type(page.locator('input[name="quota"]'), "10");
    await h.type(page.locator('input[name="days"]'), "14");
    let before = evidence.transactions.length;
    await h.click(page.getByRole("button", { name: "Create policy" }));
    await transactions(page, before, 1);
    await page.waitForFunction(() => document.querySelector("#policy")?.value, null, { timeout: 60000 });
    await h.caption("2. Reserve names and serial numbers: paste a roster CSV and click Check.");
    await h.type(page.getByLabel("Roster CSV", { exact: true }), "serial,alias\nSN-1001,line-a-001\nSN-1002,line-a-002", 25);
    await h.type(page.getByLabel("Enrollment gas per claim"), "0.005");
    await h.click(page.getByRole("button", { name: "Check", exact: true }));
    await page.getByTestId("reservation-check").waitFor();
    await h.caption("Download the credentials: the only copy of the claim keys, for your factory provisioning media.");
    const download = page.waitForEvent("download");
    await h.click(page.getByRole("button", { name: /^Download credentials for 2 devices$/ }));
    const file = await download;
    await file.delete(); // Claim keys never leave this run.
    evidence.steps.push({ step: currentStep, credentials: "downloaded and deleted unread" });
    await h.caption("3. Register the claims on chain. Each device then enrolls itself at first boot.");
    const run = page.getByRole("button", { name: /^Run \d+ pending transactions?$/ });
    await run.waitFor();
    before = evidence.transactions.length;
    await h.click(run);
    await transactions(page, before, 1, 180000);
    await page.getByRole("button", { name: "Nothing pending" }).waitFor({ timeout: 180000 }).catch(() => {});
    await page.getByTestId("status-counts").waitFor({ timeout: 60000 });
    await h.caption("Claims are registered and waiting for their devices to enroll with suifleet --enroll.");
    await h.pause(3500);
  },

  async "publish-version"(page, h) {
    await h.caption("Choose Components in the sidebar to publish software.");
    await h.click(h.sidebar("Components"));
    await page.getByRole("heading", { name: "Components", exact: true }).waitFor();
    await h.pause(1200);
    const setup = page.getByRole("button", { name: "Set up publishing" });
    const publishButton = page.getByRole("button", { name: /^Publish (version|your first version)$/ }).first();
    await Promise.race([setup.waitFor(), publishButton.waitFor()]);
    if (await setup.isVisible() || !(await publishButton.isVisible())) {
      if (!(await setup.isVisible())) await h.click(publishButton);
      await h.caption("First time only: set up your publisher catalog and approve the transaction.");
      const before = evidence.transactions.length;
      await h.click(page.getByRole("button", { name: "Set up publishing" }));
      await transactions(page, before, 1);
      await page.getByText(/Publisher catalog confirmed|Publish version/).first().waitFor({ timeout: 60000 });
    }
    const start = page.locator("#publish-heading");
    if (!(await start.isVisible())) await h.click(page.getByRole("button", { name: /^Publish (version|your first version)$/ }).first());
    await start.waitFor();
    await h.caption("Name the component and give the release a semantic version.");
    await h.type(page.getByLabel("Component name"), "guide-app");
    await h.type(page.getByLabel("Semantic version"), "1.0.0");
    await h.click(page.getByRole("button", { name: "Continue to platform artifacts" }));
    await h.caption("Choose the platform and attach the artifact for it.");
    await page.getByLabel("Operating system").first().selectOption("linux");
    await page.getByLabel("Architecture").first().selectOption("aarch64");
    await page.getByLabel("Artifact").first().setInputFiles({ name: "guide-app-1.0.0.sh", mimeType: "text/x-shellscript",
      buffer: Buffer.from("#!/bin/sh\n# SuiFleet guide example component 1.0.0\necho 'hello from guide-app 1.0.0'\n") });
    await h.pause(1200);
    await h.caption("Delivery: choose Confidential for proprietary software. This public demo artifact is published unencrypted.");
    await h.click(page.getByText("Public (unencrypted)", { exact: true }));
    await h.click(page.getByLabel(/I confirm this artifact and recipe contain nothing confidential/));
    await h.click(page.getByRole("button", { name: "Review version" }));
    await h.caption("Review, then upload to Walrus and register the version. Approve the transaction.");
    const before = evidence.transactions.length;
    await h.click(page.getByRole("button", { name: "Upload and publish version" }));
    await transactions(page, before, 1, 240000);
    await page.getByRole("heading", { name: "Version published" }).waitFor({ timeout: 120000 });
    await h.caption("Version published. It is now in the library, ready to deploy.");
    await h.pause(3500);
  },

  async "deploy-group"(page, h) {
    await h.caption("On Devices, open the Device groups tab and create a group.");
    await h.click(h.sidebar("Devices"));
    await page.getByRole("heading", { name: "Devices", exact: true }).waitFor();
    await h.click(page.getByRole("button", { name: "Device groups", exact: true }));
    await h.click(page.getByRole("button", { name: /^Create (your first )?group$/ }).first());
    await h.type(page.locator('input[name="name"]'), "guide-pilot");
    let before = evidence.transactions.length;
    await h.click(page.getByRole("button", { name: "Create", exact: true }));
    await transactions(page, before, 1);
    const row = page.getByRole("row").filter({ hasText: "guide-pilot" });
    await row.waitFor({ timeout: 90000 });
    await h.caption("Click Manage and add the device to the group.");
    await h.click(row.getByRole("button", { name: "Manage" }));
    before = evidence.transactions.length;
    const member = page.getByRole("dialog").getByText("guide-pi-001").first();
    await member.waitFor({ timeout: 60000 });
    await h.click(page.getByRole("dialog").getByRole("button", { name: "Add", exact: true }).first());
    await transactions(page, before, 1);
    await h.pause(2000);
    await page.keyboard.press("Escape");
    await h.caption("In the component library, choose Deploy this version.");
    await h.click(h.sidebar("Components"));
    await h.click(page.getByRole("link", { name: "Deploy this version" }).first());
    await page.getByRole("heading", { name: "Deploy component", exact: true }).waitFor();
    await h.caption("Select the group as the target; keep it as the group's desired version.");
    await h.click(page.getByLabel("guide-pilot", { exact: true }));
    before = evidence.transactions.length;
    await h.caption("Click Deploy and approve. Each device in the group checks its membership and applies it.");
    await h.click(page.getByRole("button", { name: /^Deploy to 1 target$/ }));
    await transactions(page, before, 1, 180000);
    await page.getByText("Deployment created", { exact: true }).waitFor({ timeout: 120000 });
    await h.caption("Deployment created. Devices report signed results under Deployments.");
    await h.click(page.getByRole("link", { name: "View deployments" }));
    await page.getByRole("heading", { name: "Deployments", exact: true }).waitFor();
    await h.pause(3500);
  },

  async "staged-rollout"(page, h, extra) {
    await h.caption("Choose Rollouts in the sidebar to release in stages.");
    await h.click(h.sidebar("Rollouts"));
    await page.getByRole("heading", { name: "Staged rollouts", exact: true }).waitFor();
    await h.pause(1500);
    await h.caption("Open Create a gradual rollout and fill in the version, recipe and digests.");
    await h.click(page.getByText("Create a gradual rollout", { exact: true }));
    const field = (label) => page.getByLabel(label, { exact: true });
    await h.type(field("component"), "guide-app");
    await h.type(field("version"), "1.0.0");
    await h.type(field("recipe"), extra.recipe, 4);
    await h.type(field("artifactSha256"), extra.artifactSha256, 4);
    await h.type(field("recipeSha256"), extra.recipeSha256, 4);
    await h.caption("List the cohorts, one line each; the first cohort goes first.");
    await h.type(page.getByLabel(/Cohorts/), extra.cohorts.join("\n"), 4);
    await h.caption("Set the pacing, bake period and abort threshold, then create the rollout.");
    await h.pause(1500);
    let before = evidence.transactions.length;
    await h.click(page.getByRole("button", { name: "Create without admitting devices" }));
    await transactions(page, before, 1);
    const overview = page.getByTestId("rollout-overview");
    await overview.waitFor({ timeout: 90000 });
    await h.caption("Select the rollout, then admit the first devices after checking device health.");
    const select = page.getByLabel("Rollout", { exact: true });
    await select.waitFor({ timeout: 60000 });
    await page.waitForFunction(() => [...document.querySelectorAll("select")].some((s) => s.options.length > 1 && [...s.options].some((o) => o.textContent === "Select…")), null, { timeout: 60000 });
    const value = await select.evaluate((s) => s.options[s.options.length - 1].value);
    await select.selectOption(value);
    await h.pause(1500);
    await h.type(page.getByLabel(/Admission candidate IDs/), extra.cohorts[0], 4);
    before = evidence.transactions.length;
    await h.click(page.getByRole("button", { name: "Check device health and continue" }));
    await transactions(page, before, 1);
    await page.getByText(/Active · cohort/).first().waitFor({ timeout: 90000 });
    await h.caption("The rollout advances only on healthy signed reports. Pause or abort at any time.");
    await h.pause(3500);
  },
};

let base;
const devices = JSON.parse(process.env.GUIDE_DEVICES || "[]");
(async () => {
  const server = await serve();
  base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || undefined, headless: true, args: ["--no-sandbox"] });
  const statePath = env("GUIDE_STATE");
  const context = await browser.newContext({ viewport: VIEWPORT, acceptDownloads: true,
    storageState: phase !== "sign-in" && fs.existsSync(statePath) ? statePath : undefined });
  await context.exposeBinding("__guideWallet", walletBinding);
  await context.addInitScript(walletScript, { address, publicKey: Array.from(keypair.getPublicKey().toSuiBytes()), chain });
  await context.addInitScript(overlayScript);
  // Record every host the console contacts besides loopback, the manifest's
  // fullnodes and Walrus endpoints, so the evidence shows what a viewer's
  // browser would reach.
  const expected = new Set(["127.0.0.1", ...(manifest.rpc_urls || [manifest.rpc_url]).map((u) => new URL(u).hostname),
    ...Object.values(manifest.walrus || {}).filter(Boolean).map((u) => new URL(u).hostname)]);
  const other = new Set();
  await context.route("**/*", (route) => {
    const host = new URL(route.request().url()).hostname;
    if (!expected.has(host)) other.add(host);
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.on("dialog", async (dialog) => { evidence.steps.push({ step: currentStep, dialog: dialog.message() }); console.log(`dialog in ${currentStep}: ${dialog.message()}`); await dialog.dismiss(); });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const recorder = new Recorder(page, await context.newCDPSession(page));
  const h = helpers(page);
  const extra = process.env.GUIDE_EXTRA ? JSON.parse(process.env.GUIDE_EXTRA) : {};
  const PHASES = { "sign-in": "sign-in", rest: "fund,register-device,enroll-batch,publish-version,deploy-group", rollout: "staged-rollout" };
  const names = (process.env.GUIDE_STEPS || PHASES[phase]).split(",").filter(Boolean);
  let failure;
  try {
    if (phase !== "sign-in") {
      await page.goto(`${base}/`);
      await page.waitForFunction(() => document.querySelector("#fleet-switcher")?.value, null, { timeout: 60000 });
    }
    for (const name of names) {
      currentStep = name;
      console.log(`recording ${name}`);
      await recorder.start(name);
      await STEPS[name](page, h, extra);
      await h.caption("");
      const frames = await recorder.stop();
      evidence.steps.push({ step: name, frames });
    }
    await context.storageState({ path: statePath });
  } catch (error) {
    failure = error;
    await page.screenshot({ path: path.join(out, `failure-${currentStep}.png`) }).catch(() => {});
    console.error(`step ${currentStep} failed: ${error.stack || error}`);
  } finally {
    evidence.page_errors = errors;
    evidence.other_hosts = [...other].sort();
    fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2));
    await browser.close();
    server.close();
  }
  if (failure) process.exit(1);
})().catch((error) => { console.error(error); process.exit(1); });
