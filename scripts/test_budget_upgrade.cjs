// SF-083 on an isolated localnet: the fleet budget introduced by a compatible
// base->head upgrade over existing registry/device objects; real draws from
// per-device GasAccounts that leave the fleet budget a read-only input (two
// devices drawing in parallel read the same budget version and each writes
// only its own account); real on-chain failures for revoked, paused,
// suspended, rate-limited, wrong-account and empty-reserve draws and for older
// package versions after a later upgrade migrates the budget; and the real
// daemon topping itself up over gRPC. Run through
// scripts/test_protocol_localnet.py --budget-upgrade (network none).
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict");
const { execFileSync, spawn } = require("node:child_process");
const { appRequire } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519"), { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet");
const { bcs } = appRequire("@mysten/sui/bcs");
const rpc = "http://127.0.0.1:9000", faucet = "http://127.0.0.1:9123", root = path.resolve(__dirname, "..");
const DAEMON = path.join(root, "suifleet-edge/target/debug/suifleet");
const SUI = 1_000_000_000n;

(async () => {
  assert.deepEqual((await fs.readdir("/sys/class/net")).sort(), ["lo"]);
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "sf083-budget-"));
  let daemon;
  try {
    const base = execFileSync("git", ["merge-base", "HEAD", "origin/main"], { encoding: "utf8" }).trim();
    const oldTree = path.join(tmp, "base"); await fs.mkdir(oldTree);
    execFileSync("tar", ["-x", "-C", oldTree], { input: execFileSync("git", ["archive", base, "contracts"], { maxBuffer: 20 * 1024 * 1024 }) });
    assert.ok(!(await fs.readdir(path.join(oldTree, "contracts/sources"))).includes("fleet_budget.move"), "base already has the budget");
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
    const owner = Ed25519Keypair.generate(), device = Ed25519Keypair.generate();
    for (const signer of [owner, device]) {
      for (let attempt = 0; attempt < 30; attempt++) {
        try { await requestSuiFromFaucetV2({ host: faucet, recipient: signer.toSuiAddress() }); break; }
        catch (error) { if (attempt === 29) throw error; await new Promise((resolve) => setTimeout(resolve, 1000)); }
      }
    }
    const digests = {}, failures = {};
    async function execute(label, transaction, signer = owner) {
      const r = await client.signAndExecuteTransaction({ transaction, signer, include: { effects: true, objectTypes: true, events: true, balanceChanges: true } });
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
      if (abort) assert.match(status, abort, `${label}: ${status}`);
      await client.waitForTransaction({ digest: result.FailedTransaction.digest });
      failures[label] = { digest: result.FailedTransaction.digest, status: result.FailedTransaction.status.error?.message ?? status };
    }
    // The exact Move abort: code and function.
    const code = (n, fn) => new RegExp(`abort code: ${n}, in '[^']*::${fn}'`);
    const shared = async (tx, id, mutable) => {
      const object = (await client.getObject({ objectId: id })).object;
      return tx.sharedObjectRef({ objectId: id, initialSharedVersion: object.owner.Shared.initialSharedVersion, mutable });
    };
    const owned = async (tx, id) => {
      const object = (await client.getObject({ objectId: id })).object;
      return tx.objectRef({ objectId: id, version: object.version, digest: object.digest });
    };

    // ── Base: a registry and an activated device before the budget exists ──
    let tx = new Transaction(); const [upgradeCapResult] = tx.publish(compile(path.join(oldTree, "contracts"))); tx.transferObjects([upgradeCapResult], owner.toSuiAddress());
    const published = await execute("publish base", tx);
    const old = published.effects.changedObjects.find((o) => o.outputState === "PackageWrite").objectId;
    const upgradeCap = created(published, "::package::UpgradeCap"), registry = created(published, "::device_registry::DeviceRegistry");
    async function register(label, address, pkg = old) {
      const t = new Transaction();
      const [d] = t.moveCall({ target: `${pkg}::device_registry::register_device`, arguments: [t.object(registry), t.pure.string(label), t.pure.address(address), t.pure.string("aarch64"), t.object("0x6")] });
      t.moveCall({ target: `${pkg}::device_cap::issue_cap`, arguments: [d] });
      const [o] = t.moveCall({ target: `${pkg}::device_registry::activate_device`, arguments: [t.object(registry), d] });
      t.transferObjects([o], owner.toSuiAddress());
      const r = await execute(`register ${label}`, t);
      return { ownerCap: created(r, "::device_registry::DeviceOwnerCap"), deviceCap: created(r, "::device_cap::DeviceCap"), deviceId: created(r, "::device_registry::Device") };
    }
    const pi = await register("pi", device.toSuiAddress());

    // ── Compatible upgrade to head ──
    let latest = old;  // Upgrades chain only from the latest version.
    async function upgrade(label, sources) {
      const tree = path.join(tmp, label); await fs.mkdir(tree);
      await fs.cp(sources, path.join(tree, "sources"), { recursive: true });
      for (const name of ["Move.toml", "Move.lock"]) await fs.copyFile(path.join(root, "contracts", name), path.join(tree, name));
      const built = compile(tree), t = new Transaction();
      const [ticket] = t.moveCall({ target: "0x2::package::authorize_upgrade", arguments: [t.object(upgradeCap), t.pure.u8(0), t.pure.vector("u8", built.digest)] });
      const receipt = t.upgrade({ modules: built.modules, dependencies: built.dependencies, package: latest, ticket });
      t.moveCall({ target: "0x2::package::commit_upgrade", arguments: [t.object(upgradeCap), receipt] });
      latest = (await execute(`upgrade ${label}`, t)).effects.changedObjects.find((o) => o.outputState === "PackageWrite").objectId;
      return latest;
    }
    const head = await upgrade("head", path.join(root, "contracts/sources"));
    const pkg = bcs.Object.parse((await client.getObject({ objectId: head, include: { objectBcs: true } })).object.objectBcs).data.Package;
    const origins = Object.fromEntries(pkg.typeOriginTable.map((o) => [`${o.moduleName}::${o.datatypeName}`, o.package]));
    assert.equal(origins["device_registry::DeviceRegistry"], old);
    for (const name of ["fleet_budget::FleetBudget", "fleet_budget::BudgetOwnerCap", "fleet_budget::GasAccount", "fleet_budget::GasDrawn"]) assert.equal(origins[name], head, name);
    console.log("PASS: compatible base-to-head upgrade introduces the budget types at head; the registry keeps its base origin");

    // The old package version has no budget: a draw through it fails on chain.
    await attack("draw through the base package", device, async (t) => {
      t.moveCall({ target: `${old}::fleet_budget::draw_gas`, arguments: [await shared(t, registry, false), await owned(t, pi.deviceCap), t.pure.u64(1), await shared(t, "0x6", false)] });
    }, /FunctionNotFound/);

    // ── Budget on existing objects ──
    const limits = (t, v = head) => t.moveCall({ target: `${v}::fleet_budget::new_limits`, arguments: [t.pure.u64(60_000_000n), t.pure.u64(3_600_000n), t.pure.u64(100_000_000n), t.pure.u64(3n), t.pure.u64(0n)] });
    tx = new Transaction();
    const [deposit] = tx.splitCoins(tx.gas, [tx.pure.u64(2n * SUI)]);
    const [budgetCapResult] = tx.moveCall({ target: `${head}::fleet_budget::create`, arguments: [tx.object(registry), deposit, limits(tx), tx.pure.u64(SUI)] });
    tx.transferObjects([budgetCapResult], owner.toSuiAddress());
    const createdBudget = await execute("create budget", tx);
    const budget = created(createdBudget, "::fleet_budget::FleetBudget"), budgetCap = created(createdBudget, "::fleet_budget::BudgetOwnerCap");
    const enroll = async (label, ownerCap, float, reserve, v = head) => {
      const t = new Transaction();
      t.moveCall({ target: `${v}::fleet_budget::enroll_device`, arguments: [t.object(budget), t.object(budgetCap), t.object(registry), t.object(ownerCap),
        t.pure.u64(float), t.pure.u64(reserve), t.object("0x6")] });
      const r = await execute(label, t);
      return { r, account: created(r, "::fleet_budget::GasAccount") };
    };
    const { r: enrolled, account: piAccount } = await enroll("enroll existing device with float and reserve", pi.ownerCap, 60_000_000n, 150_000_000n);
    const float = enrolled.balanceChanges.find((c) => c.address === device.toSuiAddress());
    assert.equal(BigInt(float.amount), 60_000_000n);
    assert.equal((await client.getObject({ objectId: piAccount })).object.owner.$kind, "Shared");
    console.log("PASS: an existing pre-upgrade device enrolls; its address receives exactly the float and its own shared GasAccount holds the reserve");

    // The budget is a read-only input of every draw: never in changedObjects,
    // listed as a read-only consensus object at an unchanged version.
    const budgetVersion = async () => (await client.getObject({ objectId: budget })).object.version;
    const readOnly = (r, label) => {
      assert.ok(!r.effects.changedObjects.some((o) => o.objectId === budget), `${label} wrote the fleet budget`);
      const read = r.effects.unchangedConsensusObjects.find((o) => o.objectId === budget);
      assert.equal(read?.kind, "ReadOnlyRoot", `${label}: ${JSON.stringify(r.effects.unchangedConsensusObjects)}`);
      return read.version;
    };
    const drawCall = (v, account, cap, amount) => (t) => t.moveCall({ target: `${v}::fleet_budget::draw_gas`,
      arguments: [t.object(budget), t.object(account), t.object(registry), t.object(cap), t.pure.u64(amount), t.object("0x6")] });
    const draw = (v = head, amount = 30_000_000n) => drawCall(v, piAccount, pi.deviceCap, amount);
    const before = await budgetVersion();
    tx = new Transaction(); draw()(tx);
    const drawn = await execute("device draw", tx, device);
    assert.ok(drawn.events.some((e) => e.eventType.endsWith("::fleet_budget::GasDrawn")));
    assert.equal(readOnly(drawn, "device draw"), before);
    assert.equal(await budgetVersion(), before);
    assert.ok(drawn.effects.changedObjects.some((o) => o.objectId === piAccount && o.outputState === "ObjectWrite"));
    const drawCost = drawn.effects.gasUsed;
    const drawNet = BigInt(drawCost.computationCost) + BigInt(drawCost.storageCost) - BigInt(drawCost.storageRebate);
    // The sender pays storage for its rewritten account and receives its old rebate.
    console.log(`PASS: the device draws from its own account; the fleet budget is a read-only input at an unchanged version (computation ${drawCost.computationCost}, net ${drawNet} MIST)`);

    const attackDraw = (label, abort, v = head, amount = 1n, account = piAccount) => attack(label, device, async (t) => {
      t.moveCall({ target: `${v}::fleet_budget::draw_gas`, arguments: [await shared(t, budget, false), await shared(t, account, true), await shared(t, registry, false),
        await owned(t, pi.deviceCap), t.pure.u64(amount), await shared(t, "0x6", false)] });
    }, abort);
    await attackDraw("draw above the float cap", code(8, "fleet_budget::draw_gas"), head, 60_000_001n);
    // Revocation stops draws immediately; the compromised key retries.
    tx = new Transaction(); tx.moveCall({ target: `${head}::device_registry::revoke_device`, arguments: [tx.object(registry), tx.object(pi.ownerCap)] }); await execute("revoke device", tx);
    await attackDraw("draw after revocation", code(2, "device_cap::assert_active"));
    tx = new Transaction(); tx.moveCall({ target: `${head}::device_registry::reinstate_device`, arguments: [tx.object(registry), tx.object(pi.ownerCap)] }); await execute("reinstate device", tx);
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_budget::set_paused`, arguments: [tx.object(budget), tx.object(budgetCap), tx.pure.bool(true)] }); await execute("pause budget", tx);
    await attackDraw("draw while paused", code(3, "fleet_budget::assert_may_draw"));
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_budget::set_paused`, arguments: [tx.object(budget), tx.object(budgetCap), tx.pure.bool(false)] }); await execute("resume budget", tx);
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_budget::set_device_suspended`, arguments: [tx.object(budget), tx.object(budgetCap), tx.object(piAccount), tx.pure.bool(true)] });
    const suspended = await execute("suspend device", tx);
    readOnly(suspended, "suspend device");
    await attackDraw("draw while suspended", code(7, "fleet_budget::assert_may_draw"));
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_budget::set_device_suspended`, arguments: [tx.object(budget), tx.object(budgetCap), tx.object(piAccount), tx.pure.bool(false)] }); await execute("resume device", tx);
    // The device cannot withdraw: only the owner cap can, and the network
    // refuses a transaction using an object the sender does not own.
    {
      const t = new Transaction(); t.setSender(device.toSuiAddress()); t.setGasBudget(50_000_000);
      t.moveCall({ target: `${head}::fleet_budget::withdraw`, arguments: [await shared(t, budget, true), await owned(t, budgetCap), t.pure.u64(1n)] });
      await assert.rejects(client.signAndExecuteTransaction({ transaction: t, signer: device }), /not signed by the correct sender|owned by account address/);
      failures["device withdraws with the owner's cap"] = { digest: null, status: "rejected before execution: the owner cap is not the device's object" };
    }
    // Maximum rate: the allowance (100M) and draw count (3) per period.
    tx = new Transaction(); draw(head, 60_000_000n)(tx); await execute("second draw", tx, device);
    await attackDraw("draw above the period allowance", code(9, "fleet_budget::draw_gas"), head, 20_000_000n);
    tx = new Transaction(); draw(head, 10_000_000n)(tx); await execute("third draw", tx, device);
    await attackDraw("fourth draw in the period", code(10, "fleet_budget::draw_gas"));
    console.log("PASS: on-chain failures for above-float, revoked, paused, suspended, device-withdraw, over-allowance and over-count draws");

    // ── Separate accounts: parallel draws, wrong accounts, empty reserves ──
    const third = Ed25519Keypair.generate(), fourth = Ed25519Keypair.generate();
    for (const signer of [third, fourth]) await requestSuiFromFaucetV2({ host: faucet, recipient: signer.toSuiAddress() });
    const pa = await register("pa", third.toSuiAddress(), head), pb = await register("pb", fourth.toSuiAddress(), head);
    const { account: aAccount } = await enroll("enroll device A", pa.ownerCap, 0n, 100_000_000n);
    const { account: bAccount } = await enroll("enroll device B", pb.ownerCap, 0n, 20_000_000n);
    const version = await budgetVersion();
    const [ra, rb] = await Promise.all([
      (async () => { const t = new Transaction(); drawCall(head, aAccount, pa.deviceCap, 30_000_000n)(t); return execute("device A draws", t, third); })(),
      (async () => { const t = new Transaction(); drawCall(head, bAccount, pb.deviceCap, 20_000_000n)(t); return execute("device B draws", t, fourth); })(),
    ]);
    // Both read the same budget version: neither waited on the other's write.
    assert.equal(readOnly(ra, "device A draw"), version);
    assert.equal(readOnly(rb, "device B draw"), version);
    assert.ok(!ra.effects.changedObjects.some((o) => o.objectId === bAccount) && !rb.effects.changedObjects.some((o) => o.objectId === aAccount));
    assert.equal(await budgetVersion(), version);
    console.log(`PASS: two devices drew in parallel; both read budget version ${version} and each wrote only its own account`);
    // B emptied its own reserve; the budget still holds funds, out of B's reach.
    await attack("draw from an emptied reserve", fourth, async (t) => {
      t.moveCall({ target: `${head}::fleet_budget::draw_gas`, arguments: [await shared(t, budget, false), await shared(t, bAccount, true), await shared(t, registry, false),
        await owned(t, pb.deviceCap), t.pure.u64(1n), await shared(t, "0x6", false)] });
    }, code(11, "fleet_budget::draw_gas"));
    // B's key cannot draw from A's account.
    await attack("draw from another device's account", fourth, async (t) => {
      t.moveCall({ target: `${head}::fleet_budget::draw_gas`, arguments: [await shared(t, budget, false), await shared(t, aAccount, true), await shared(t, registry, false),
        await owned(t, pb.deviceCap), t.pure.u64(1n), await shared(t, "0x6", false)] });
    }, code(16, "fleet_budget::assert_may_draw"));
    // The owner refills B's reserve from the budget, and B draws again.
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_budget::fund_reserve`, arguments: [tx.object(budget), tx.object(budgetCap), tx.object(bAccount), tx.pure.u64(20_000_000n)] }); await execute("owner refills a reserve", tx);
    // Any address may declare a shared object mutable at its own gas cost; an
    // adversarial device doing so still gets only what its limits allow, and
    // the budget's contents stay as they were.
    const u64 = bcs.u64(), Budget = bcs.struct("FleetBudget", { id: bcs.Address, version: u64, registry_id: bcs.Address, owner_cap_id: bcs.Address, balance: u64,
      paused: bcs.bool(), default_limits: bcs.fixedArray(5, u64), low_balance_threshold: u64, device_count: u64, total_deposited: u64, total_withdrawn: u64,
      total_paid: u64, total_allocated: u64, total_reclaimed: u64 });
    const balanceOf = async () => BigInt(Budget.parse((await client.getObject({ objectId: budget, include: { content: true } })).object.content).balance);
    const held = await balanceOf();
    {
      const t = new Transaction(); t.setSender(fourth.toSuiAddress());
      t.moveCall({ target: `${head}::fleet_budget::draw_gas`, arguments: [await shared(t, budget, true), await shared(t, bAccount, true), await shared(t, registry, false),
        await owned(t, pb.deviceCap), t.pure.u64(10_000_000n), await shared(t, "0x6", false)] });
      await execute("adversarial draw declaring the budget mutable", t, fourth);
    }
    assert.equal(await balanceOf(), held);
    await attack("adversarial draw beyond its reserve", fourth, async (t) => {
      t.moveCall({ target: `${head}::fleet_budget::draw_gas`, arguments: [await shared(t, budget, true), await shared(t, bAccount, true), await shared(t, registry, false),
        await owned(t, pb.deviceCap), t.pure.u64(20_000_000n), await shared(t, "0x6", false)] });
    }, code(11, "fleet_budget::draw_gas"));
    // Removal retires the account and returns its reserve; it never draws again.
    tx = new Transaction(); tx.moveCall({ target: `${head}::fleet_budget::remove_device`, arguments: [tx.object(budget), tx.object(budgetCap), tx.object(aAccount)] });
    const removed = await execute("owner removes device A", tx);
    assert.ok(removed.events.some((e) => e.eventType.endsWith("::fleet_budget::ReserveChanged")));
    await attack("draw from a removed device's account", third, async (t) => {
      t.moveCall({ target: `${head}::fleet_budget::draw_gas`, arguments: [await shared(t, budget, false), await shared(t, aAccount, true), await shared(t, registry, false),
        await owned(t, pa.deviceCap), t.pure.u64(1n), await shared(t, "0x6", false)] });
    }, code(4, "fleet_budget::assert_current"));
    console.log("PASS: on-chain failures for an emptied reserve, another device's account and a removed account; an adversarial mutable budget input changed nothing and stayed within limits");

    // ── A later upgrade retires this version's draws ──
    const future = path.join(tmp, "future-sources"); await fs.cp(path.join(root, "contracts/sources"), future, { recursive: true });
    const source = path.join(future, "fleet_budget.move"), text = await fs.readFile(source, "utf8");
    assert.ok(text.includes("const VERSION: u64 = 1;"));
    await fs.writeFile(source, text.replace("const VERSION: u64 = 1;", "const VERSION: u64 = 2;"));
    const next = await upgrade("future", future);
    tx = new Transaction(); tx.moveCall({ target: `${next}::fleet_budget::migrate`, arguments: [tx.object(budget), tx.object(budgetCap)] }); await execute("migrate budget", tx);
    tx = new Transaction(); tx.moveCall({ target: `${next}::fleet_budget::fund_device`, arguments: [tx.object(budget), tx.object(budgetCap), tx.object(registry), tx.object(piAccount), tx.pure.u64(1n)] }); await execute("owner refill via the newest version", tx);
    // New period not reached: test only the version gate with a fresh device.
    const second = Ed25519Keypair.generate();
    await requestSuiFromFaucetV2({ host: faucet, recipient: second.toSuiAddress() });
    const pi2 = await register("pi2", second.toSuiAddress(), next);
    const { account: pi2Account } = await enroll("enroll second device", pi2.ownerCap, 0n, 10_000_000n, next);
    await attack("draw through the previous version after migration", second, async (t) => {
      t.moveCall({ target: `${head}::fleet_budget::draw_gas`, arguments: [await shared(t, budget, false), await shared(t, pi2Account, true), await shared(t, registry, false),
        await owned(t, pi2.deviceCap), t.pure.u64(1n), await shared(t, "0x6", false)] });
    }, code(0, "fleet_budget::assert_may_draw"));
    tx = new Transaction(); drawCall(next, pi2Account, pi2.deviceCap, 1_000_000n)(tx);
    await execute("draw through the newest version", tx, second);
    console.log("PASS: after a later upgrade migrates the budget, the previous version's draw fails on chain and the newest succeeds");

    // ── The real daemon tops itself up over gRPC ──
    const key = path.join(tmp, "daemon.key");
    const daemonAddress = execFileSync(DAEMON, ["--provision", key], { encoding: "utf8" }).trim();
    await requestSuiFromFaucetV2({ host: faucet, recipient: daemonAddress });
    const pi3 = await register("pi3", daemonAddress, next);
    await enroll("enroll daemon device", pi3.ownerCap, 1_000_000n, 200_000_000n, next);
    const resolved = JSON.parse(execFileSync("python3", ["-c", `
import json, sys; sys.path.insert(0, "scripts"); import deployment_manifest as dm
from types import SimpleNamespace
a = SimpleNamespace(chain_identifier=dm.chain_facts(sys.argv[1])["chain_identifier"], rpc=sys.argv[1], package=sys.argv[2], upgrade_cap=sys.argv[3], registry=sys.argv[4], network="localnet", publisher="http://127.0.0.1:9", aggregator="http://127.0.0.1:9")
print(json.dumps(dm.resolve(a)))`, rpc, next, upgradeCap, registry], { cwd: root, encoding: "utf8" }));
    for (const name of JSON.parse(await fs.readFile(path.join(root, "interfaces/fleet-v1.json"), "utf8")).optional_type_origins.fleet_budget) assert.ok(resolved.type_origins[name], name);
    await fs.writeFile(path.join(tmp, "deployment.json"), JSON.stringify(resolved));
    const state = path.join(tmp, "state");
    await fs.writeFile(path.join(tmp, "config.toml"), `[sui]\nmanifest_path = "deployment.json"\n[device]\nkeypair_path = ${JSON.stringify(key)}\ndevice_object_id = "${pi3.deviceId}"\ndevice_cap_id = "${pi3.deviceCap}"\n[apply]\ncommand = "/bin/false"\ntimeout_seconds = 5\n[runtime]\nstate_dir = ${JSON.stringify(state)}\npoll_seconds = 1\n[budget]\nbudget_id = "${budget}"\nlow_water_mist = ${100_000n * SUI}\ndraw_mist = 40000000\n`);
    const log = await fs.open(path.join(tmp, "daemon.log"), "w");
    daemon = spawn(DAEMON, [], { env: { ...process.env, EDGE_CONFIG: path.join(tmp, "config.toml"), RUST_LOG: process.env.RUST_LOG ?? "info" }, stdio: ["ignore", log.fd, log.fd] });
    const journal = async () => { try { return JSON.parse(await fs.readFile(path.join(state, "state.json"), "utf8")); } catch { return {}; } };
    let funding;
    for (let i = 0; i < 240 && !(funding = (await journal()).funding)?.last_draw_digest; i++) await new Promise((resolve) => setTimeout(resolve, 1000));
    if (!funding?.last_draw_digest) { await log.close(); throw new Error(`daemon did not draw by ${new Date().toISOString()}; journal ${JSON.stringify(await journal())}\n` + (await fs.readFile(path.join(tmp, "daemon.log"), "utf8")).slice(-6000)); }
    const effects = await client.getTransaction({ digest: funding.last_draw_digest, include: { effects: true, events: true } });
    assert.ok(effects.Transaction.status.success);
    // The daemon's draw (and the signer policy) take the budget read-only.
    readOnly(effects.Transaction, "daemon draw");
    assert.ok(effects.Transaction.events.some((e) => e.eventType.endsWith("::fleet_budget::GasDrawn")));
    assert.equal(BigInt(funding.last_draw_mist), 40_000_000n);
    digests["daemon draw"] = funding.last_draw_digest;
    // Its allowance is 100M with 3 draws: it keeps drawing until a limit, then records why.
    for (let i = 0; i < 90 && !(funding = (await journal()).funding)?.blocked; i++) await new Promise((resolve) => setTimeout(resolve, 1000));
    assert.match(funding.blocked ?? "", /used up|draw limit/, JSON.stringify(funding));
    daemon.kill("SIGINT"); await new Promise((resolve) => daemon.once("exit", resolve)); daemon = undefined; await log.close();
    console.log(`PASS: the real daemon read its balance, budget and account over gRPC, drew ${funding.draws} time(s) with checked effects, then journaled: ${funding.blocked}`);
    console.log(JSON.stringify({ result: "PASS", base, old_package: old, head_package: head, next_package: next, budget, draw_net_mist: drawNet.toString(), digests, failures }));
  } finally {
    if (daemon) daemon.kill("SIGKILL");
    await fs.rm(tmp, { recursive: true, force: true });
  }
})().catch((e) => { console.error(e.stack); process.exitCode = 1; });
