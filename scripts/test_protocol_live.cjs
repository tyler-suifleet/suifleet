// Real generated call encoders against a disposable JSON-RPC localnet.
// Transport migration acceptance belongs to SF-003/SF-004; this verifies SF-005.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { SuiClient } = require("../dapp/node_modules/@mysten/sui/client");
const { Transaction } = require("../dapp/node_modules/@mysten/sui/transactions");
const { Ed25519Keypair } = require("../dapp/node_modules/@mysten/sui/keypairs/ed25519");
const { load } = require("./test_protocol.cjs");

(async () => {
  assert.equal(process.env.PROTOCOL_TEST_DISPOSABLE, "1", "use the isolated test_protocol_localnet.py runner");
  const rpc = process.env.PROTOCOL_TEST_RPC || "http://127.0.0.1:9000";
  const faucet = process.env.PROTOCOL_TEST_FAUCET || "http://127.0.0.1:9123";
  for (const url of [rpc, faucet]) assert.ok(["localhost", "127.0.0.1"].includes(new URL(url).hostname), "disposable loopback endpoints only");
  const client = new SuiClient({url:rpc});
  const operator = new Ed25519Keypair();
  const owner = operator.toSuiAddress();
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "protocol-live-"));
  let stage = "funding";
  try {
    let faucetAccepted = false;
    for (let attempt = 0; attempt < 60; attempt++) {
      if (BigInt((await client.getBalance({owner})).totalBalance) > 0n) break;
      try {
        if (!faucetAccepted) {
          const response = await fetch(`${faucet}/v1/gas`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({FixedAmountRequest:{recipient:owner}})});
          faucetAccepted = response.ok;
          if (!response.ok && response.status !== 503) throw new Error(`faucet HTTP ${response.status}`);
        }
      } catch (error) { if (attempt === 59) throw error; }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    assert.ok(BigInt((await client.getBalance({owner})).totalBalance) > 0n);
    stage = "compile";
    const contracts = path.join(temp, "contracts");
    await fs.mkdir(contracts);
    for (const item of ["Move.toml", "Move.lock", "sources"]) await fs.cp(path.resolve(__dirname, "../contracts", item), path.join(contracts, item), {recursive:true});
    const compileEnv = {...process.env, SUI_CONFIG_DIR:path.join(temp, "compiler-config")};
    await fs.mkdir(compileEnv.SUI_CONFIG_DIR);
    // No compiler wallet or key generation: explicit empty keystore and loopback env.
    const keystore = path.join(compileEnv.SUI_CONFIG_DIR, "sui.keystore");
    await fs.writeFile(keystore, "[]\n");
    await fs.writeFile(path.join(compileEnv.SUI_CONFIG_DIR, "client.yaml"),
      `keystore:\n  File: ${keystore}\nenvs:\n- alias: testnet\n  rpc: ${rpc}\n  ws: null\n  basic_auth: null\nactive_env: testnet\nactive_address: null\n`);
    // Keep the source's compiler environment; disable RPC dependency tree shaking.
    let output;
    try {
      output = execFileSync("sui", ["move", "build", "--path", contracts, "--build-env", "testnet", "--no-tree-shaking", "--dump-bytecode-as-base64"], {env:compileEnv,encoding:"utf8",maxBuffer:20*1024*1024,stdio:["ignore","pipe","pipe"]});
    } catch (error) {
      // Never echo CLI output: future tools might still attempt key initialization.
      const text = String(error.stdout || "") + String(error.stderr || "");
      const categories = [
        ["environment-missing", /is not present in Move.toml/],
        ["config-missing", /No sui config found/],
        ["network", /connect|Connection|DNS|transport|RPC|rpc/],
        ["compile", /error\[|Compilation|compilation/],
        ["lock", /lock|Lock/],
      ].filter(([,pattern]) => pattern.test(text)).map(([label]) => label).join(",");
      throw new Error(`isolated Move build failed (exit ${error.status}, categories=${categories || "other"}); CLI output suppressed`);
    }
    const compiled = JSON.parse(output.slice(output.indexOf("{")));
    async function execute(transaction) {
      const result = await client.signAndExecuteTransaction({transaction,signer:operator,options:{showEffects:true,showObjectChanges:true}});
      assert.equal(result.effects.status.status, "success", result.effects.status.error);
      await client.waitForTransaction({digest:result.digest});
      return result;
    }
    async function object(id, options) {
      for (let attempt = 0; attempt < 100; attempt++) {
        const result = await client.getObject({id,options});
        if (result.data) return result;
        if (!["notExists", "objectNotExists"].includes(result.error?.code)) throw new Error(`object read failed: ${result.error?.code}`);
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error("published object did not become indexed");
    }
    stage = "publish execution";
    const publish = new Transaction();
    const [cap] = publish.publish(compiled);
    publish.transferObjects([cap], publish.pure.address(owner));
    const result = await execute(publish);
    stage = "publish metadata";
    const packageId = result.objectChanges.find(change => change.type === "published").packageId;
    const registryId = result.objectChanges.find(change => change.type === "created" && change.objectType.endsWith("::device_registry::DeviceRegistry")).objectId;
    const capId = result.objectChanges.find(change => change.type === "created" && change.objectType.endsWith("::package::UpgradeCap")).objectId;
    stage = "package lookup";
    const packageObject = await object(packageId, {showBcs:true});
    stage = "registry lookup";
    const registry = await object(registryId, {showOwner:true});
    stage = "manifest";
    const manifest = {
      schema_version:1, protocol_version:1, network:"localnet", chain_identifier:await client.getChainIdentifier(),rpc_url:rpc,
      call_package_id:packageId,package_version:String(packageObject.data.version),upgrade_cap_id:capId,
      type_origins:Object.fromEntries(packageObject.data.bcs.typeOriginTable.map(entry => [`${entry.module_name}::${entry.datatype_name}`,entry.package])),
      shared_objects:{registry:{id:registryId,initial_shared_version:String(registry.data.owner.Shared.initial_shared_version)}},
      supported_clients:{dapp:{min:"0.0.1",max_exclusive:"0.0.2"},edge:{min:"0.1.0",max_exclusive:"0.2.0"}},
      walrus:{publisher_url:"http://127.0.0.1:31415",aggregator_url:"http://127.0.0.1:31416"},
    };
    process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
    const app = load("dapp/src/lib/sui.ts");
    const protocol = load("dapp/src/lib/fleet-protocol.ts");
    stage = "enroll";
    await execute(app.buildRegisterDeviceTx("ABI device", owner, "aarch64", owner));
    const devices = await app.fetchDevices(client, owner);
    assert.equal(devices.length, 1);
    stage = "group";
    await execute(app.buildCreateGroupTx("ABI lab", "compiled interface acceptance"));
    let groups = await app.fetchGroups(client, owner);
    assert.equal(groups.length, 1);
    await execute(app.buildAddDeviceToGroupTx(groups[0].objectId, devices[0].objectId));
    groups = await app.fetchGroups(client, owner);
    assert.deepEqual(groups[0].deviceIds, [devices[0].objectId]);
    stage = "deployment";
    const created = await execute(app.buildCreateDeploymentTx("test-app", "v1", "recipe", "ab".repeat(32), "cd".repeat(32), [groups[0].objectId], [devices[0].objectId]));
    const deployment = created.objectChanges.find(change => change.type === "created" && change.objectType.endsWith("::deployment_manager::DeploymentRecord")).objectId;
    const caps = await client.getOwnedObjects({owner,filter:{StructType:`${packageId}::device_cap::DeviceCap`}});
    stage = "receipt";
    const report = new Transaction();
    protocol.calls.deployment_manager_report_status(report, packageId, deployment, caps.data[0].data.objectId, protocol.STATUS.applied);
    await execute(report);
    const receipts = await app.readReceipts(client, deployment);
    assert.equal(receipts.verified, true);
    assert.deepEqual(receipts.statuses, {[devices[0].objectId]:protocol.STATUS.applied});
    stage = "cleanup";
    await execute(app.buildRemoveDeviceFromGroupTx(groups[0].objectId, devices[0].objectId));
    await execute(app.buildDeleteDeploymentTx(deployment));
    await execute(app.buildDeleteGroupTx(groups[0].objectId));
    await execute(app.buildDeregisterDeviceTx(devices[0].objectId));
    console.log(`PASS: generated publish/enroll/group/deployment/report/cleanup calls executed with successful effects; chain=${manifest.chain_identifier}, package=${packageId}`);
  } catch (error) {
    console.error(`FAIL: protocol localnet at ${stage}: ${error.message}`);
    // Stack locations only; never CLI output, transaction payloads or key material.
    for (const line of String(error.stack || "").split("\n").slice(1, 6)) {
      if (/^\s+at .*:\d+:\d+\)?$/.test(line)) console.error(line);
    }
    process.exitCode = 1;
  } finally { await fs.rm(temp, {recursive:true,force:true}); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
