// SF-130 on an isolated localnet: per-fleet cost settings introduced by a
// compatible base->head upgrade and stored on a FleetBudget created before
// it; owner-only changes with auditable FleetSettingsChanged events carrying
// the previous values; real on-chain refusals for another fleet's cap and
// out-of-bounds values; base-package budget operations still working beside
// the record; and the real daemon and the dApp reading the record over gRPC.
// Run through scripts/test_protocol_localnet.py --settings-upgrade (network none).
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const { appRequire, loader } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519"), { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet");
const { bcs } = appRequire("@mysten/sui/bcs"), { normalizeSuiAddress } = appRequire("@mysten/sui/utils");
const rpc = "http://127.0.0.1:9000", faucet = "http://127.0.0.1:9123", root = path.resolve(__dirname, "..");
const DAEMON = path.join(root, "suifleet-edge/target/debug/suifleet");
const SUI = 1_000_000_000n, MINUTE = 60_000n, DAY = 86_400_000n;

(async () => {
  assert.deepEqual((await fs.readdir("/sys/class/net")).sort(), ["lo"]);
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "sf130-settings-"));
  let daemon;
  try {
    const base = execFileSync("git", ["merge-base", "HEAD", "origin/main"], { encoding: "utf8" }).trim();
    const oldTree = path.join(tmp, "base"); await fs.mkdir(oldTree);
    execFileSync("tar", ["-x", "-C", oldTree], { input: execFileSync("git", ["archive", base, "contracts"], { maxBuffer: 20 * 1024 * 1024 }) });
    const baseSources = await fs.readdir(path.join(oldTree, "contracts/sources"));
    assert.ok(baseSources.includes("fleet_budget.move") && !baseSources.includes("fleet_settings.move"), "base must have the budget and not the settings");
    const env = { ...process.env, SUI_CONFIG_DIR: path.join(tmp, "wallet") }; await fs.mkdir(env.SUI_CONFIG_DIR);
    const keystore = path.join(env.SUI_CONFIG_DIR, "sui.keystore"); await fs.writeFile(keystore, "[]");
    await fs.writeFile(path.join(env.SUI_CONFIG_DIR, "client.yaml"), `keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: ${rpc}\nactive_env: testnet\nactive_address: null\n`);
    function compile(directory) {
      let text;
      try { text = execFileSync("sui", ["move", "build", "--path", directory, "--build-env", "testnet", "--no-tree-shaking", "--dump-bytecode-as-base64"], { env, encoding: "utf8", maxBuffer: 20 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] }); }
      catch { throw Error("isolated compilation failed; wallet output suppressed"); }
      return JSON.parse(text.slice(text.indexOf("{")));
    }
    const client = new SuiGrpcClient({ network: "localnet", baseUrl: rpc });
    const owner = Ed25519Keypair.generate(), stranger = Ed25519Keypair.generate();
    for (const signer of [owner, stranger]) {
      for (let attempt = 0; attempt < 30; attempt++) {
        try { await requestSuiFromFaucetV2({ host: faucet, recipient: signer.toSuiAddress() }); break; }
        catch (error) { if (attempt === 29) throw error; await new Promise((resolve) => setTimeout(resolve, 1000)); }
      }
    }
    const digests = {}, failures = {};
    async function execute(label, transaction, signer = owner) {
      const r = await client.signAndExecuteTransaction({ transaction, signer, include: { effects: true, objectTypes: true, events: true } });
      assert.ok(r.Transaction?.status.success, `${label}: ${JSON.stringify(r.FailedTransaction?.status)}`);
      await client.waitForTransaction({ digest: r.Transaction.digest });
      digests[label] = r.Transaction.digest;
      return r.Transaction;
    }
    const created = (r, suffix) => Object.keys(r.objectTypes).find((id) => r.objectTypes[id].endsWith(suffix));
    // Fully resolved bytes bypass simulation, so a refused call fails with chain effects.
    async function attack(label, signer, build, abort) {
      const gas = (await client.listCoins({ owner: signer.toSuiAddress() })).objects[0];
      const tx = new Transaction(); tx.setSender(signer.toSuiAddress());
      tx.setGasPayment([{ objectId: gas.objectId, version: gas.version, digest: gas.digest }]);
      tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice); tx.setGasBudget(50_000_000);
      await build(tx);
      const bytes = await tx.build(), signed = await signer.signTransaction(bytes);
      const result = await client.executeTransaction({ transaction: bytes, signatures: [signed.signature], include: { effects: true } });
      assert.equal(result.$kind, "FailedTransaction", `${label} succeeded`);
      const status = JSON.stringify(result.FailedTransaction.status);
      assert.match(status, abort, `${label}: ${status}`);
      await client.waitForTransaction({ digest: result.FailedTransaction.digest });
      failures[label] = { digest: result.FailedTransaction.digest, status: result.FailedTransaction.status.error?.message ?? status };
    }
    const code = (n, fn) => new RegExp(`abort code: ${n}, in '[^']*::${fn}'`);
    const shared = async (tx, id, mutable) => {
      const object = (await client.getObject({ objectId: id })).object;
      return tx.sharedObjectRef({ objectId: id, initialSharedVersion: object.owner.Shared.initialSharedVersion, mutable });
    };
    const owned = async (tx, id) => {
      const object = (await client.getObject({ objectId: id })).object;
      return tx.objectRef({ objectId: id, version: object.version, digest: object.digest });
    };

    // ── Base: a fleet budget that exists before the settings ──
    let tx = new Transaction(); const [upgradeCapResult] = tx.publish(compile(path.join(oldTree, "contracts"))); tx.transferObjects([upgradeCapResult], owner.toSuiAddress());
    const published = await execute("publish base", tx);
    const old = published.effects.changedObjects.find((o) => o.outputState === "PackageWrite").objectId;
    const upgradeCap = created(published, "::package::UpgradeCap"), registry = created(published, "::device_registry::DeviceRegistry");
    const limits = (t, v) => t.moveCall({ target: `${v}::fleet_budget::new_limits`, arguments: [t.pure.u64(60_000_000n), t.pure.u64(3_600_000n), t.pure.u64(100_000_000n), t.pure.u64(3n), t.pure.u64(0n)] });
    async function createBudget(label, signer, v) {
      const t = new Transaction();
      const [deposit] = t.splitCoins(t.gas, [t.pure.u64(SUI)]);
      const [cap] = t.moveCall({ target: `${v}::fleet_budget::create`, arguments: [t.object(registry), deposit, limits(t, v), t.pure.u64(0n)] });
      t.transferObjects([cap], signer.toSuiAddress());
      const r = await execute(label, t, signer);
      return { budget: created(r, "::fleet_budget::FleetBudget"), cap: created(r, "::fleet_budget::BudgetOwnerCap") };
    }
    const fleet = await createBudget("create budget on the base package", owner, old);

    // ── Compatible upgrade to head ──
    const tree = path.join(tmp, "head"); await fs.mkdir(tree);
    await fs.cp(path.join(root, "contracts/sources"), path.join(tree, "sources"), { recursive: true });
    for (const name of ["Move.toml", "Move.lock"]) await fs.copyFile(path.join(root, "contracts", name), path.join(tree, name));
    const built = compile(tree);
    tx = new Transaction();
    const [ticket] = tx.moveCall({ target: "0x2::package::authorize_upgrade", arguments: [tx.object(upgradeCap), tx.pure.u8(0), tx.pure.vector("u8", built.digest)] });
    const receipt = tx.upgrade({ modules: built.modules, dependencies: built.dependencies, package: old, ticket });
    tx.moveCall({ target: "0x2::package::commit_upgrade", arguments: [tx.object(upgradeCap), receipt] });
    const head = (await execute("upgrade to head", tx)).effects.changedObjects.find((o) => o.outputState === "PackageWrite").objectId;
    const pkg = bcs.Object.parse((await client.getObject({ objectId: head, include: { objectBcs: true } })).object.objectBcs).data.Package;
    const origins = Object.fromEntries(pkg.typeOriginTable.map((o) => [`${o.moduleName}::${o.datatypeName}`, o.package]));
    const protocol = JSON.parse(await fs.readFile(path.join(root, "interfaces/fleet-v1.json"), "utf8"));
    for (const name of protocol.optional_type_origins.fleet_settings) assert.equal(origins[name], head, name);
    assert.equal(origins["fleet_budget::FleetBudget"], old, "the budget keeps its base origin");
    console.log("PASS: compatible base-to-head upgrade introduces the settings types at head; the existing budget type keeps its base origin");

    // The base package has no settings module.
    await attack("set settings through the base package", owner, async (t) => {
      t.moveCall({ target: `${old}::fleet_settings::set`, arguments: [await shared(t, fleet.budget, true), await owned(t, fleet.cap),
        t.pure.u64(1_800_000n), t.pure.u64(60_000n), t.pure.u64(DAY), t.pure.u64(7n * DAY), t.pure.u32(3), await shared(t, "0x6", false)] });
    }, /FunctionNotFound|ModuleNotFound|LINKER_ERROR|VMVerificationOrDeserializationError/);

    // ── The real daemon on the existing fleet, before any record ──
    const key = path.join(tmp, "daemon.key");
    const daemonAddress = execFileSync(DAEMON, ["--provision", key], { encoding: "utf8" }).trim();
    tx = new Transaction();
    const [device] = tx.moveCall({ target: `${head}::device_registry::register_device`, arguments: [tx.object(registry), tx.pure.string("pi"), tx.pure.address(daemonAddress), tx.pure.string("aarch64"), tx.object("0x6")] });
    tx.moveCall({ target: `${head}::device_cap::issue_cap`, arguments: [device] });
    const [deviceOwner] = tx.moveCall({ target: `${head}::device_registry::activate_device`, arguments: [tx.object(registry), device] });
    tx.transferObjects([deviceOwner], owner.toSuiAddress());
    const registered = await execute("register daemon device", tx);
    const deviceId = created(registered, "::device_registry::Device"), deviceCap = created(registered, "::device_cap::DeviceCap");
    const resolved = JSON.parse(execFileSync("python3", ["-c", `
import json, sys; sys.path.insert(0, "scripts"); import deployment_manifest as dm
from types import SimpleNamespace
a = SimpleNamespace(chain_identifier=dm.chain_facts(sys.argv[1])["chain_identifier"], rpc=sys.argv[1], package=sys.argv[2], upgrade_cap=sys.argv[3], registry=sys.argv[4], network="localnet", publisher="http://127.0.0.1:9", aggregator="http://127.0.0.1:9")
print(json.dumps(dm.resolve(a)))`, rpc, head, upgradeCap, registry], { cwd: root, encoding: "utf8" }));
    for (const name of protocol.optional_type_origins.fleet_settings) assert.equal(normalizeSuiAddress(resolved.type_origins[name]), normalizeSuiAddress(head), name);
    await fs.writeFile(path.join(tmp, "deployment.json"), JSON.stringify(resolved));
    const state = path.join(tmp, "state"), logPath = path.join(tmp, "daemon.log");
    await fs.writeFile(path.join(tmp, "config.toml"), `[sui]\nmanifest_path = "deployment.json"\n[device]\nkeypair_path = ${JSON.stringify(key)}\ndevice_object_id = "${deviceId}"\ndevice_cap_id = "${deviceCap}"\n[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\n[runtime]\nstate_dir = ${JSON.stringify(state)}\npoll_seconds = 1\n[budget]\nbudget_id = "${fleet.budget}"\nlow_water_mist = 0\n`);
    const log = await fs.open(logPath, "w");
    daemon = spawn(DAEMON, [], { env: { ...process.env, EDGE_CONFIG: path.join(tmp, "config.toml"), RUST_LOG: process.env.RUST_LOG ?? "info", NO_COLOR: "1" }, stdio: ["ignore", log.fd, log.fd] });
    const daemonLog = async () => (await fs.readFile(logPath, "utf8")).replace(/\x1b\[[0-9;]*m/g, "");
    const journal = async () => { try { return JSON.parse(await fs.readFile(path.join(state, "state.json"), "utf8")); } catch { return {}; } };
    const waitFor = async (condition, message, seconds = 60) => {
      for (let i = 0; i < seconds * 4; i++) { if (await condition()) return; await new Promise((resolve) => setTimeout(resolve, 250)); }
      throw new Error(`${message}\n${(await daemonLog()).slice(-4000)}`);
    };
    await waitFor(async () => (await journal()).initialized, "daemon did not start polling");
    await new Promise((resolve) => setTimeout(resolve, 3000));
    assert.ok(!(await daemonLog()).includes("Fleet settings applied"), "no record yet");

    // ── Owner sets the record on the pre-upgrade budget ──
    const load = loader();
    const { fleet_settings_FleetSettingsChangedBcs: Changed } = load(path.join(root, "dapp/src/lib/fleet-protocol.ts"));
    const set = (values, v = head) => (t) => t.moveCall({ target: `${v}::fleet_settings::set`, arguments: [t.object(fleet.budget), t.object(fleet.cap),
      t.pure.u64(values[0]), t.pure.u64(values[1]), t.pure.u64(values[2]), t.pure.u64(values[3]), t.pure.u32(values[4]), t.object("0x6")] });
    const changedEvent = (r) => {
      const events = r.events.filter((e) => e.eventType.endsWith("::fleet_settings::FleetSettingsChanged"));
      assert.equal(events.length, 1);
      assert.equal(normalizeSuiAddress(events[0].eventType.split("::")[0]), normalizeSuiAddress(head));
      return Changed.parse(events[0].bcs);
    };
    const first = [15n * MINUTE, 30_000n, 6n * 60n * MINUTE, DAY, 13];
    tx = new Transaction(); set(first)(tx);
    let event = changedEvent(await execute("owner sets fleet settings", tx));
    assert.equal(normalizeSuiAddress(event.budget_id), normalizeSuiAddress(fleet.budget));
    assert.equal(normalizeSuiAddress(event.changed_by), normalizeSuiAddress(owner.toSuiAddress()));
    assert.equal(event.previous, null);
    assert.deepEqual([event.settings.check_interval_ms, event.settings.rollout_check_interval_ms, event.settings.health_interval_ms,
      event.settings.archive_interval_ms, event.settings.archive_retention_epochs], first.map(String).map((v, i) => i === 4 ? Number(v) : v));
    await waitFor(async () => (await daemonLog()).includes(`Fleet settings applied scope="checks" check_interval_ms=900000 rollout_check_interval_ms=30000 health_interval_ms=21600000 archive_interval_ms=86400000 archive_retention_epochs=13`),
      "daemon did not apply the chain record", 30);
    console.log("PASS: the owner set the record on a pre-upgrade budget with an auditable event (no previous); the real daemon read it over gRPC at its next check");

    // ── A change carries the previous values; the dApp reads the record ──
    const second = [30n * MINUTE, 0n, DAY, 7n * DAY, 3];
    tx = new Transaction(); set(second)(tx);
    event = changedEvent(await execute("owner changes fleet settings", tx));
    assert.equal(event.previous.check_interval_ms, String(first[0]));
    assert.equal(event.settings.rollout_check_interval_ms, "0");
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(resolved);
    const settings = loader()(path.join(root, "dapp/src/lib/fleet-settings.ts"));
    assert.deepEqual(await settings.fetchFleetSettings(client, fleet.budget), { checkIntervalMs: 1_800_000, rolloutCheckIntervalMs: 0,
      healthIntervalMs: 86_400_000, archiveIntervalMs: 604_800_000, archiveRetentionEpochs: 3 });
    const otherFleet = await createBudget("create another fleet's budget", stranger, head);
    assert.equal(await settings.fetchFleetSettings(client, otherFleet.budget), undefined, "a fleet without a record");
    console.log("PASS: a change records the previous values; the dApp reads the record (and its absence) through gRPC");

    // ── Refusals on chain ──
    await attack("another fleet's owner cap", stranger, async (t) => {
      t.moveCall({ target: `${head}::fleet_settings::set`, arguments: [await shared(t, fleet.budget, true), await owned(t, otherFleet.cap),
        t.pure.u64(1_800_000n), t.pure.u64(60_000n), t.pure.u64(DAY), t.pure.u64(7n * DAY), t.pure.u32(3), await shared(t, "0x6", false)] });
    }, code(1, "fleet_budget::assert_owner"));
    for (const [label, values] of [["check interval below 5 minutes", [299_999n, 0n, DAY, 7n * DAY, 3]],
      ["fast check slower than routine", [5n * MINUTE, 6n * MINUTE, DAY, 7n * DAY, 3]],
      ["retention beyond 53 epochs", [30n * MINUTE, MINUTE, DAY, 7n * DAY, 54]]]) {
      await attack(label, owner, async (t) => {
        t.moveCall({ target: `${head}::fleet_settings::set`, arguments: [await shared(t, fleet.budget, true), await owned(t, fleet.cap),
          t.pure.u64(values[0]), t.pure.u64(values[1]), t.pure.u64(values[2]), t.pure.u64(values[3]), t.pure.u32(values[4]), await shared(t, "0x6", false)] });
      }, code(0, "fleet_settings::new"));
    }
    assert.equal((await settings.fetchFleetSettings(client, fleet.budget)).checkIntervalMs, 1_800_000, "refused writes left the record unchanged");
    console.log("PASS: on-chain refusals for another fleet's cap and out-of-bounds values; the record is unchanged");

    // ── Existing budget operations still work beside the record ──
    tx = new Transaction();
    const [coin] = tx.splitCoins(tx.gas, [tx.pure.u64(1_000n)]);
    tx.moveCall({ target: `${old}::fleet_budget::deposit`, arguments: [tx.object(fleet.budget), tx.object(fleet.cap), coin] });
    await execute("deposit through the base package", tx);
    daemon.kill("SIGINT"); await new Promise((resolve) => daemon.once("exit", resolve)); daemon = undefined; await log.close();
    console.log("PASS: base-package budget operations still succeed on the budget holding the record");
    console.log(JSON.stringify({ result: "PASS", base, old_package: old, head_package: head, budget: fleet.budget, digests, failures }));
  } finally {
    if (daemon) daemon.kill("SIGKILL");
    await fs.rm(tmp, { recursive: true, force: true });
  }
})().catch((e) => { console.error(e.stack); process.exitCode = 1; });
