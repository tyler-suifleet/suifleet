// SF-132: real frontend batch enrollment and gas/size measurement.
// Run via test_protocol_localnet.py --budget-enrollment in a network-none container.
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict"), { execFileSync } = require("node:child_process");
const { appRequire, loader } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions"), { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { fromBase58, toHex, normalizeStructTag } = appRequire("@mysten/sui/utils"), { bcs } = appRequire("@mysten/sui/bcs"), { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet");
const rpc = process.env.FLEET_TEST_RPC || "http://127.0.0.1:9000", faucet = process.env.FLEET_TEST_FAUCET || "http://127.0.0.1:9123";
for (const endpoint of [rpc, faucet]) assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(new URL(endpoint).hostname), "refusing public-network publication");
assert.equal(process.env.PROTOCOL_TEST_DISPOSABLE, "1", "use the disposable network-none localnet wrapper");
let stage = "startup";
(async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "suifleet-frontend-live-"));
  try {
    const client = new SuiGrpcClient({ network: "localnet", baseUrl: rpc }), operator = Ed25519Keypair.generate(), deviceSigner = Ed25519Keypair.generate();
    for (const signer of [operator, deviceSigner]) {
      for (let attempt = 0; attempt < 30; attempt++) {
        if ((await client.listCoins({ owner: signer.toSuiAddress() })).objects.length) break;
        try { await requestSuiFromFaucetV2({ host: faucet, recipient: signer.toSuiAddress() }); break; }
        catch (error) { if (attempt === 29) throw error; await new Promise((resolve) => setTimeout(resolve, 1000)); }
      }
    }
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
    async function execute(transaction, signer = operator) {
      const result = await client.signAndExecuteTransaction({ transaction, signer, include: { effects: true, objectTypes: true, bcs: true, events: true } });
      if (result.FailedTransaction) throw new Error(result.FailedTransaction.status.error?.message || "execution failed");
      assert.equal(result.Transaction.status.success, true);
      await checkpointed(result.Transaction.digest);
      return result.Transaction;
    }
    async function checkpointed(digest) {
      // History queries and gas references see checkpointed/indexed state.
      await client.waitForTransaction({ digest });
      for (let attempt = 0; attempt < 100; attempt++) {
        const confirmed = await client.getTransaction({ digest });
        if (confirmed.Transaction?.checkpoint != null || confirmed.FailedTransaction?.checkpoint != null) return;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      throw new Error("transaction did not become checkpointed for history reads");
    }
    const publish = new Transaction(), [cap] = publish.publish(compiled);
    publish.transferObjects([cap], operator.toSuiAddress());
    stage = "publish"; const published = await execute(publish);
    const packageId = published.effects.changedObjects.find((object) => object.outputState === "PackageWrite").objectId;
    const types = published.objectTypes;
    const registryId = Object.keys(types).find((id) => types[id].endsWith("::device_registry::DeviceRegistry"));
    const capId = Object.keys(types).find((id) => types[id].includes("::") && normalizeStructTag(types[id]) === normalizeStructTag("0x2::package::UpgradeCap"));
    assert.ok(registryId && capId);
    stage = "package metadata"; const { object: packageObject } = await client.getObject({ objectId: packageId, include: { objectBcs: true } });
    const raw = bcs.Object.parse(packageObject.objectBcs).data.Package;
    stage = "registry metadata"; const { object: registry } = await client.getObject({ objectId: registryId });
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
    const load = loader(), budgetLib = load("lib/fleet-budget.ts"), protocol = load("lib/fleet-protocol.ts");
    const batchLib = load("lib/budget-enrollment.ts");
    const evidence = { network: "disposable network-none localnet", recorded_at: new Date().toISOString(),
      compiler: execFileSync("sui", ["--version"], { encoding: "utf8" }).trim(),
      node: "sui 1.82.1-987123842623 (checksum-verified by localnet wrapper)",
      chunks: [], device_count: 1000 };
    // Register and activate in bounded setup PTBs. No 1000 approval claim is
    // made for registration; only enrollment wallet approvals are measured.
    const devices = [];
    for (let start = 0; start < 1000; start += 100) {
      const tx = new Transaction();
      const owners = [];
      for (let i = start; i < start + 100; i++) {
        const [device] = protocol.calls.device_registry_register_device(tx, packageId, registryId, `batch-${i}`, deviceSigner.toSuiAddress(), "aarch64", "0x6");
        protocol.calls.device_cap_issue_cap(tx, packageId, device);
        const [owner] = protocol.calls.device_registry_activate_device(tx, packageId, registryId, device);
        owners.push(owner);
      }
      tx.transferObjects(owners, operator.toSuiAddress());
      stage = `register ${start}`;
      const result = await execute(tx);
      const ids = Object.keys(result.objectTypes).filter(id => result.objectTypes[id].endsWith("::device_registry::DeviceOwnerCap"));
      for (const ownerCapId of ids) {
        const { object } = await client.getObject({ objectId: ownerCapId, include: { content: true } });
        const owner = protocol.device_registry_DeviceOwnerCapBcs.parse(object.content);
        devices.push({ objectId: owner.device_id, ownerCapId });
      }
    }
    assert.equal(devices.length, 1000);
    const limits = { ...budgetLib.DEFAULT_LIMITS, floatCap: 1000n, periodAllowance: 2000n };
    stage = "create budget";
    await execute(budgetLib.buildCreateBudgetTx(10000000n, limits, 0n, operator.toSuiAddress()));
    const budget = (await budgetLib.fetchBudgets(client, operator.toSuiAddress()))[0];
    const reads = batchLib.enrollmentReads(client, operator.toSuiAddress(), budget.id);
    let approvals = 0, reject = true;
    const options = { devices, ...reads,
      prepare: tx => batchLib.prepareEnrollment(tx, client, operator.toSuiAddress()),
      execute: async tx => {
        if (reject && approvals === 1) throw new Error("fixture user rejection");
        approvals++;
        const bytes = await tx.build({ client });
        const r = await execute(tx);
        const g = r.effects.gasUsed;
        evidence.chunks.push({ digest: r.digest, devices: tx.getData().commands.length, bytes: bytes.length,
          gas_budget_mist: tx.getData().gasData.budget,
          computation_mist: g.computationCost, storage_mist: g.storageCost, rebate_mist: g.storageRebate,
          net_gas_mist: (BigInt(g.computationCost) + BigInt(g.storageCost) - BigInt(g.storageRebate)).toString() });
        return { digest: r.digest };
      }, onProgress: () => {},
    };
    stage = "partial enrollment";
    await assert.rejects(batchLib.enrollBudgetDevices(options), /fixture user rejection/);
    assert.equal((await reads.readBudget()).deviceCount, 200n);
    reject = false;
    stage = "resume enrollment";
    const resumed = await batchLib.enrollBudgetDevices(options);
    assert.equal(resumed.skipped, 200); assert.equal(resumed.confirmed, 800);
    assert.equal(approvals, 5);
    const final = await reads.readBudget();
    assert.equal(final.deviceCount, 1000n);
    assert.equal(final.totalPaid, 1000000n); assert.equal(final.totalAllocated, 4000000n);
    // All dynamic-field lookups are verified, including actual reserves.
    for (let i = 0; i < devices.length; i += 100) {
      const accounts = await Promise.all(devices.slice(i, i+100).map(d => budgetLib.fetchAccount(client, budget.id, d.objectId)));
      assert(accounts.every(a => a && a.reserve === 4000n));
    }
    const repeat = await batchLib.enrollBudgetDevices(options);
    assert.equal(repeat.skipped, 1000); assert.equal(approvals, 5);
    evidence.approvals_per_1000 = approvals;
    evidence.partial_failure_resume = "200 confirmed, rejected second chunk, resumed 800; complete rerun requests zero approvals";
    console.log(JSON.stringify(evidence, null, 2));
    if (process.env.BATCH_EVIDENCE) await fs.writeFile(process.env.BATCH_EVIDENCE, JSON.stringify(evidence, null, 2) + "\n");
    console.log("PASS: actual frontend 1000-device batch enrollment, effects, size/gas, rejection and resume");
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
})().catch(e => { console.error(`Stage ${stage}:`, e); process.exitCode = 1; });
