// SF-171 acceptance on a network-none disposable chain: a real base->head
// upgrade, then the dApp's batch builder against pre-upgrade customer objects,
// on-chain batch limit and architecture refusals, mixed old/new package calls
// and fleet accounting after bulk removal.
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { appRequire, loader } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions");
const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519"), { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet");
const { bcs } = appRequire("@mysten/sui/bcs"), { fromBase58, toHex, normalizeSuiAddress: id } = appRequire("@mysten/sui/utils");
const root = path.resolve(__dirname, ".."), rpc = "http://127.0.0.1:9000", faucet = "http://127.0.0.1:9123";
(async () => {
  assert.deepEqual((await fs.readdir("/sys/class/net")).sort(), ["lo"]);
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "sf171-batch-"));
  try {
    const base = process.env.SF171_BASE_REF || "origin/main";
    const oldTree = path.join(tmp, "base"); await fs.mkdir(oldTree);
    execFileSync("tar", ["-x", "-C", oldTree], { input: execFileSync("git", ["archive", base, "contracts"], { maxBuffer: 20 * 1024 * 1024 }) });
    assert.ok(!(await fs.readFile(path.join(oldTree, "contracts/sources/customer.move"), "utf8")).includes("add_many_to_group"), "base must precede SF-171");
    const env = { ...process.env, SUI_CONFIG_DIR: path.join(tmp, "wallet") }; await fs.mkdir(env.SUI_CONFIG_DIR);
    const keystore = path.join(env.SUI_CONFIG_DIR, "empty.keystore"); await fs.writeFile(keystore, "[]");
    await fs.writeFile(path.join(env.SUI_CONFIG_DIR, "client.yaml"), `keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: ${rpc}\nactive_env: testnet\nactive_address: null\n`);
    const compile = (directory) => {
      let text; try { text = execFileSync("sui", ["move", "build", "--path", directory, "--build-env", "testnet", "--no-tree-shaking", "--dump-bytecode-as-base64"], { env, encoding: "utf8", maxBuffer: 25 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] }); }
      catch (e) { throw Error(`compile failed (${path.basename(directory)}): ${String(e.stderr).slice(-1500)}`); }
      return JSON.parse(text.slice(text.indexOf("{")));
    };
    const client = new SuiGrpcClient({ network: "localnet", baseUrl: rpc });
    const owner = Ed25519Keypair.generate();
    for (let n = 0; n < 6; n++) for (let retry = 0; ; retry++) {
      try { await requestSuiFromFaucetV2({ host: faucet, recipient: owner.toSuiAddress() }); break; }
      catch (error) { if (retry === 9) throw error; await new Promise(resolve => setTimeout(resolve, 500)); }
    }
    const digests = {}, failures = {};
    async function execute(label, tx) {
      tx.setGasBudget(label.startsWith("upgrade") || label.startsWith("publish") ? 2_000_000_000 : 400_000_000);
      const r = await client.signAndExecuteTransaction({ transaction: tx, signer: owner, include: { effects: true, events: true, objectTypes: true } });
      assert.ok(r.Transaction?.status.success, `${label}: ${JSON.stringify(r.FailedTransaction?.status)}`);
      await client.waitForTransaction({ digest: r.Transaction.digest }); digests[label] = r.Transaction.digest; console.log(`PASS localnet: ${label}`); return r.Transaction;
    }
    // Refusals are signed execution effects, not resolver simulation.
    async function reject(label, tx, expected) {
      const gas = (await client.listCoins({ owner: owner.toSuiAddress() })).objects[0];
      tx.setSender(owner.toSuiAddress()); tx.setGasPayment([{ objectId: gas.objectId, version: gas.version, digest: gas.digest }]);
      tx.setGasPrice((await client.getReferenceGasPrice()).referenceGasPrice); tx.setGasBudget(50_000_000);
      // Resolve inputs from the reviewed ABI (exact shared mutability) so the SDK does not simulate.
      const abi = JSON.parse(await fs.readFile("interfaces/fleet-v1.json", "utf8")).modules, data = tx.getData(), mutable = new Set();
      for (const { MoveCall: call } of data.commands) call.arguments.forEach((arg, i) => {
        if (arg.$kind === "Input" && abi[call.module].functions[call.function].parameters[i].startsWith("&mut ")) mutable.add(arg.Input);
      });
      for (let i = 0; i < data.inputs.length; i++) {
        const objectId = data.inputs[i].UnresolvedObject?.objectId; if (!objectId) continue;
        const { object } = await client.getObject({ objectId });
        data.inputs[i] = object.owner.$kind === "Shared" ? { Object: { SharedObject: { objectId, initialSharedVersion: object.owner.Shared.initialSharedVersion, mutable: mutable.has(i) } } }
          : { Object: { ImmOrOwnedObject: { objectId, version: object.version, digest: object.digest } } };
      }
      const bytes = await Transaction.from(JSON.stringify(data)).build(), signed = await owner.signTransaction(bytes);
      const r = await client.executeTransaction({ transaction: bytes, signatures: [signed.signature], include: { effects: true } });
      assert.ok(r.FailedTransaction, `${label} unexpectedly succeeded`); await client.waitForTransaction({ digest: r.FailedTransaction.digest });
      const status = r.FailedTransaction.status.error?.message ?? JSON.stringify(r.FailedTransaction.status);
      assert.match(status, new RegExp(`abort code: ${expected[0]}, in '[^']*::${expected[1]}'`), `${label}: ${status}`);
      failures[label] = { digest: r.FailedTransaction.digest, status }; console.log(`PASS localnet: ${label} refused`);
    }
    const created = (r, suffix) => Object.keys(r.objectTypes).filter(o => r.objectTypes[o].endsWith(suffix));
    let tx = new Transaction(); const [publishCap] = tx.publish(compile(path.join(oldTree, "contracts"))); tx.transferObjects([publishCap], owner.toSuiAddress());
    const published = await execute("publish base", tx), old = published.effects.changedObjects.find(o => o.outputState === "PackageWrite").objectId;
    const [registry] = created(published, "::device_registry::DeviceRegistry"), [upgradeCap] = created(published, "::package::UpgradeCap");
    // Pre-upgrade customer, fleet, devices and group: existing objects for the new entry points.
    const oldCall = (module, fn, args) => { const t = new Transaction(); t.moveCall({ target: `${old}::${module}::${fn}`, arguments: args(t) }); return t; };
    const cr = await execute("base customer", oldCall("customer", "create", t => [t.object(registry), t.pure.string("Batch customer")]));
    const [customer] = created(cr, "::customer::Customer"), [fleet] = created(cr, "::customer::Fleet"), [budget] = created(cr, "::fleet_budget::FleetBudget");
    await execute("base entitlement", oldCall("customer", "set_entitlement", t => [t.object(customer), t.object(registry), t.pure.u64(8), t.pure.u64(10_000_000), t.pure.u64(1)]));
    const archs = ["x86_64", "x86_64", "x86_64", "aarch64", "x86_64"];
    tx = new Transaction();
    archs.forEach((arch, i) => tx.moveCall({ target: `${old}::customer::register_device`, arguments: [tx.object(customer), tx.object(fleet), tx.object(budget), tx.object(registry),
      tx.pure.string(`batch-${i}`), tx.pure.address(Ed25519Keypair.generate().toSuiAddress()), tx.pure.string(arch), tx.object("0x6")] }));
    const registered = await execute("base register five devices", tx);
    const deviceIds = registered.events.filter(e => e.eventType.endsWith("::device_registry::DeviceRegistered")).map(e => bcs.struct("R", { device_id: bcs.Address }).parse(e.bcs).device_id);
    assert.equal(deviceIds.length, 5);
    const gr = await execute("base managed group", oldCall("customer", "create_group", t => [t.object(customer), t.object(fleet), t.pure.string("Canary"), t.pure.string(""), t.object("0x6")]));
    const [group] = created(gr, "::device_group::DeviceGroup");
    await execute("base single membership", oldCall("customer", "add_to_group", t => [t.object(customer), t.object(fleet), t.object(group), t.pure.id(deviceIds[4])]));

    const built = compile(path.join(root, "contracts")); tx = new Transaction();
    const [ticket] = tx.moveCall({ target: "0x2::package::authorize_upgrade", arguments: [tx.object(upgradeCap), tx.pure.u8(0), tx.pure.vector("u8", built.digest)] });
    const receipt = tx.upgrade({ modules: built.modules, dependencies: built.dependencies, package: old, ticket });
    tx.moveCall({ target: "0x2::package::commit_upgrade", arguments: [tx.object(upgradeCap), receipt] });
    const head = (await execute("upgrade head", tx)).effects.changedObjects.find(o => o.outputState === "PackageWrite").objectId;
    const pkg = bcs.Object.parse((await client.getObject({ objectId: head, include: { objectBcs: true } })).object.objectBcs).data.Package;
    const origins = Object.fromEntries(pkg.typeOriginTable.map(o => [`${o.moduleName}::${o.datatypeName}`, o.package]));
    assert.equal(origins["customer::Customer"], old); assert.equal(origins["device_group::DeviceGroup"], old);
    assert.equal(origins["customer::GroupBatchApplied"], head, "the batch event type is introduced by the upgrade");

    // The dApp's real manifest-driven builder against the upgraded package.
    const manifest = JSON.parse(await fs.readFile("manifests/fixture.localnet.json", "utf8"));
    Object.assign(manifest, { network: "localnet", rpc_url: rpc, call_package_id: head, upgrade_cap_id: upgradeCap, package_version: String(pkg.version), type_origins: origins });
    manifest.chain_identifier = toHex(fromBase58((await client.ledgerService.getServiceInfo({})).response.chainId).slice(0, 4));
    manifest.shared_objects.registry = { id: registry, initial_shared_version: (await client.getObject({ objectId: registry })).object.owner.Shared.initialSharedVersion };
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
    const load = loader(), batch = load("lib/group-batch.ts"), app = load("lib/sui.ts"), fleets = load("lib/customer-fleets.ts");
    await load("lib/deployment-manifest.ts").verifyDeployment(client);
    assert.equal(batch.groupBatchSupported(), true);
    const named = (await fleets.fetchCustomerFleets(client, owner.toSuiAddress())).find(f => f.id === id(fleet));
    const devices = await app.fetchFleetDevices(client, named.devices);
    const readGroups = async () => (await app.fetchGroups(client, id(fleet))).filter(g => g.objectId === id(group));
    let groups = await readGroups();
    assert.deepEqual(groups[0].deviceIds, [id(deviceIds[4])]);

    // Review: the aarch64 device differs from the group's x86_64 member and is excluded by default.
    const all = deviceIds.map(d => id(d));
    const plan = batch.planMembership("add", all, [id(group)], devices, groups, { managed: true });
    assert.deepEqual(plan.mismatches.map(m => m.deviceId), [id(deviceIds[3])]);
    assert.equal(plan.changes.length, 3); assert.equal(plan.transactions.length, 1);
    const added = await execute("dApp batch add three reviewed x86_64 devices", batch.buildMembershipTx("add", plan.transactions[0], devices, named));
    assert.equal(added.events.filter(e => e.eventType.endsWith("::device_group::DeviceAddedToGroup")).length, 3);
    const applied = added.events.filter(e => e.eventType === `${head}::customer::GroupBatchApplied`).map(e => load("lib/fleet-protocol.ts").customer_GroupBatchAppliedBcs.parse(e.bcs));
    assert.deepEqual(applied.map(a => [a.group_id, a.added, a.count]), [[id(group), true, "3"]]);
    groups = await readGroups();
    assert.equal(groups[0].deviceIds.length, 4);

    // The chain re-checks the reviewed architecture and the batch bound.
    const pinned = (ids, arch) => { const t = new Transaction(); load("lib/fleet-protocol.ts").calls.customer_add_many_to_group(t, head, customer, fleet, group, registry, ids, arch); return t; };
    await reject("architecture mismatch in a pinned batch", pinned([deviceIds[3]], "x86_64"), [8, "customer::add_many_to_group"]);
    await reject("unregistered ID in a pinned batch", pinned([id("0xbad")], "x86_64"), [8, "customer::add_many_to_group"]);
    await reject("batch above the bound", pinned(Array.from({ length: 101 }, (_, i) => id(`0x${(i + 1).toString(16)}`)), null), [7, "customer::assert_batch"]);
    await reject("duplicate member in a batch", pinned([deviceIds[3], deviceIds[3]], "aarch64"), [1, "device_group::add_managed"]);
    // An explicitly included mismatch is pinned to its own architecture.
    const mixed = batch.planMembership("add", all, [id(group)], devices, groups, { managed: true, includeMismatched: true });
    assert.deepEqual(mixed.changes.map(c => c.deviceId), [id(deviceIds[3])]);
    await execute("dApp batch add explicitly included aarch64 device", batch.buildMembershipTx("add", mixed.transactions[0], devices, named));
    // Mixed versions: the base package's single-device entry point still works on the same group.
    await execute("base package single removal after upgrade", oldCall("customer", "remove_from_group", t => [t.object(customer), t.object(fleet), t.object(group), t.pure.id(deviceIds[4])]));
    groups = await readGroups();
    const removal = batch.planMembership("remove", all, [id(group)], devices, groups, { managed: true });
    assert.equal(removal.changes.length, 4); assert.equal(removal.skipped.length, 1);
    await execute("dApp batch remove four devices", batch.buildMembershipTx("remove", removal.transactions[0], devices, named));
    groups = await readGroups();
    assert.deepEqual(groups[0].deviceIds, []);
    // Group counts returned to zero: the empty group deletes and a device can leave the fleet.
    await execute("delete emptied group", oldCall("customer", "delete_group", t => [t.object(customer), t.object(fleet), t.object(group)]));
    const account = await client.getDynamicField({ parentId: budget, name: { type: `${old}::fleet_budget::DeviceKey`, bcs: bcs.Address.serialize(deviceIds[0]).toBytes() } });
    await execute("deregister a formerly grouped device", oldCall("customer", "deregister_device", t => [t.object(customer), t.object(fleet), t.object(budget),
      t.object(bcs.Address.parse(account.dynamicField.value.bcs)), t.object(registry), t.pure.id(deviceIds[0])]));
    console.log(JSON.stringify({ result: "PASS", base, old_package: old, head_package: head, customer, fleet, group, digests, failures }));
  } finally { await fs.rm(tmp, { recursive: true, force: true }); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
