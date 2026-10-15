// Real gRPC fleet transactions and reads on a disposable loopback node only.
const fs = require("node:fs/promises"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict"), { execFileSync } = require("node:child_process");
const { appRequire, loader } = require("./frontend_test_utils.cjs");
const { SuiGrpcClient } = appRequire("@mysten/sui/grpc"), { Transaction } = appRequire("@mysten/sui/transactions"), { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
const { fromBase58, toHex, normalizeStructTag } = appRequire("@mysten/sui/utils"), { bcs } = appRequire("@mysten/sui/bcs"), { requestSuiFromFaucetV2 } = appRequire("@mysten/sui/faucet");
const rpc = process.env.FLEET_TEST_RPC || "http://127.0.0.1:9000", faucet = process.env.FLEET_TEST_FAUCET || "http://127.0.0.1:9123";
for (const endpoint of [rpc, faucet]) assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(new URL(endpoint).hostname), "refusing public-network publication");
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
      const result = await client.signAndExecuteTransaction({ transaction, signer, include: { effects: true, objectTypes: true } });
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
    const sui = loader()("lib/sui.ts");
    stage = "register device"; await execute(sui.buildRegisterDeviceTx("Pi acceptance", deviceSigner.toSuiAddress(), "aarch64", operator.toSuiAddress()));
    stage = "device inventory"; const devices = await sui.fetchDevices(client, operator.toSuiAddress());
    assert.equal(devices.length, 1); assert.equal(devices[0].deviceAddress, deviceSigner.toSuiAddress()); assert.equal(devices[0].arch, "aarch64");
    assert.ok(devices[0].ownerCapId, "registration activates the device into registry custody");
    stage = "create group"; await execute(sui.buildCreateGroupTx("Lab", "gRPC acceptance"));
    stage = "group history"; let groups = await sui.fetchGroups(client, operator.toSuiAddress());
    assert.equal(groups.length, 1);
    stage = "group membership"; await execute(sui.buildAddDeviceToGroupTx(groups[0].objectId, devices[0]));
    groups = await sui.fetchGroups(client, operator.toSuiAddress()); assert.deepEqual(groups[0].deviceIds, [devices[0].objectId]);
    stage = "reissue cap"; await execute(sui.buildIssueCapTx(devices[0]));
    const caps = await client.listOwnedObjects({ owner: deviceSigner.toSuiAddress(), type: `${packageId}::device_cap::DeviceCap` });
    assert.equal(caps.objects.length, 2);
    stage = "create deployment"; await execute(sui.buildCreateDeploymentTx("test-app", "1.0.0", "fixture-recipe", "ab".repeat(32), "cd".repeat(32), [groups[0].objectId], [devices[0].objectId]));
    stage = "deployment history"; let deployments = await sui.fetchDeploymentEvents(client, operator.toSuiAddress());
    assert.equal(deployments.length, 1); assert.equal(deployments[0].receiptsVerified, true); assert.deepEqual(deployments[0].statuses, {});
    const control = loader()("lib/deployment-control.ts");
    stage = "pause deployment";
    await execute(control.buildDeploymentControlTx(deployments[0].deploymentId, { ...control.immediateControl, state: 1, notice_ms: "5000" }));
    assert.equal((await control.readDeploymentControl(client, deployments[0].deploymentId)).state, 1);
    stage = "resume deployment";
    await execute(control.buildDeploymentControlTx(deployments[0].deploymentId, { ...control.immediateControl, state: 0 }));
    assert.equal((await control.readDeploymentControl(client, deployments[0].deploymentId)).revision, "2");
    stage = "cancel deployment";
    await execute(control.buildDeploymentControlTx(deployments[0].deploymentId, { ...control.immediateControl, state: 2 }));
    assert.equal((await sui.fetchDeploymentEvents(client, operator.toSuiAddress()))[0].control.state, 2);
    // Bypass preflight simulation to require real failed execution effects for
    // irreversible cancellation, using only this disposable signer's gas.
    const { object: cancelledRecord } = await client.getObject({ objectId: deployments[0].deploymentId });
    const cancelGas = (await client.listCoins({ owner: operator.toSuiAddress() })).objects[0];
    const { referenceGasPrice: cancelPrice } = await client.getReferenceGasPrice();
    const resume = new Transaction(); resume.setSender(operator.toSuiAddress());
    resume.setGasPayment([{ objectId: cancelGas.objectId, version: cancelGas.version, digest: cancelGas.digest }]);
    resume.setGasPrice(cancelPrice); resume.setGasBudget(100000000);
    resume.moveCall({ target: `${packageId}::deployment_manager::set_control`, arguments: [
      resume.sharedObjectRef({ objectId: cancelledRecord.objectId, initialSharedVersion: cancelledRecord.owner.Shared.initialSharedVersion, mutable: true }),
      resume.pure.u8(0), ...Array.from({length:5}, () => resume.pure.u64(0)),
    ] });
    const resumeBytes = await resume.build(), resumeSignature = await operator.signTransaction(resumeBytes);
    stage = "cancelled resume execution";
    const rejectedResume = await client.executeTransaction({ transaction: resumeBytes, signatures: [resumeSignature.signature], include: {effects:true} });
    assert.equal(rejectedResume.$kind, "FailedTransaction");
    await checkpointed(rejectedResume.FailedTransaction.digest);
    assert.equal((await control.readDeploymentControl(client, deployments[0].deploymentId)).state, 2);
    console.log(`PASS: real gRPC control pause/resume/cancel and irreversible cancellation (failed execution ${rejectedResume.FailedTransaction.digest})`);
    const report = new Transaction();
    const protocol = loader()("lib/fleet-protocol.ts");
    protocol.calls.deployment_manager_report_verified_status(report, packageId, deployments[0].deploymentId, registryId, caps.objects[0].objectId, protocol.STATUS.applied);
    stage = "report status"; await execute(report, deviceSigner);
    deployments = await sui.fetchDeploymentEvents(client, operator.toSuiAddress());
    assert.deepEqual(deployments[0].statuses, { [devices[0].objectId]: 1 }); assert.deepEqual(deployments[0].resolvedDeviceIds, [devices[0].objectId]);
    // A non-owner group mutation must return failed execution effects.
    // Build complete bytes offline so automatic simulation cannot stop this
    // intentional abort before it reaches on-chain execution.
    const { object: sharedGroup } = await client.getObject({ objectId: groups[0].objectId });
    const gas = (await client.listCoins({ owner: deviceSigner.toSuiAddress() })).objects[0];
    const { referenceGasPrice } = await client.getReferenceGasPrice();
    const forbidden = new Transaction(); forbidden.setSender(deviceSigner.toSuiAddress());
    forbidden.setGasPayment([{ objectId: gas.objectId, version: gas.version, digest: gas.digest }]);
    forbidden.setGasPrice(referenceGasPrice); forbidden.setGasBudget(100000000);
    forbidden.moveCall({ target: `${packageId}::device_group::remove_device`, arguments: [
      forbidden.sharedObjectRef({ objectId: sharedGroup.objectId, initialSharedVersion: sharedGroup.owner.Shared.initialSharedVersion, mutable: true }),
      forbidden.pure(bcs.Address.serialize(devices[0].objectId)),
    ] });
    const bytes = await forbidden.build(), signed = await deviceSigner.signTransaction(bytes);
    stage = "failed execution"; const failed = await client.executeTransaction({ transaction: bytes, signatures: [signed.signature], include: { effects: true } });
    assert.equal(failed.$kind, "FailedTransaction");
    await checkpointed(failed.FailedTransaction.digest);
    // Contract creation accepts arbitrary IDs; reproduce the review's history regression.
    const validDeployment = deployments[0].deploymentId;
    stage = "foreign group";
    await execute(sui.buildRegisterDeviceTx("foreign member", deviceSigner.toSuiAddress(), "aarch64", deviceSigner.toSuiAddress()), deviceSigner);
    async function indexedFirst(query, label) {
      for (let attempt = 0; attempt < 100; attempt++) {
        const items = await query();
        if (items.length) return items[0];
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error(`${label} never became indexed after checkpointed execution`);
    }
    const foreignDevice = await indexedFirst(() => sui.fetchDevices(client, deviceSigner.toSuiAddress()), "foreign device");
    await execute(sui.buildCreateGroupTx("foreign lab", "owned by another creator"), deviceSigner);
    const foreignGroup = await indexedFirst(() => sui.fetchGroups(client, deviceSigner.toSuiAddress()), "foreign group");
    await execute(sui.buildAddDeviceToGroupTx(foreignGroup.objectId, foreignDevice), deviceSigner);
    stage = "arbitrary target history";
    await execute(sui.buildCreateDeploymentTx("invalid-targets", "1.0.0", "fixture-recipe", "ab".repeat(32), "cd".repeat(32), [devices[0].objectId, foreignGroup.objectId]));
    deployments = await sui.fetchDeploymentEvents(client, operator.toSuiAddress());
    assert.equal(deployments.length, 2);
    assert.deepEqual(deployments.find(deployment => deployment.componentName === "invalid-targets").resolvedDeviceIds, []);
    assert.deepEqual(deployments.find(deployment => deployment.deploymentId === validDeployment).statuses, { [devices[0].objectId]: 1 });
    stage = "deregister device"; await execute(sui.buildDeregisterDeviceTx(foreignDevice), deviceSigner);
    assert.deepEqual(await sui.fetchDevices(client, deviceSigner.toSuiAddress()), []);
    stage = "delete deployment";
    for (const deployment of deployments) await execute(sui.buildDeleteDeploymentTx(deployment.deploymentId, deployment.schemaVersion));
    assert.deepEqual(await sui.fetchDeploymentEvents(client, operator.toSuiAddress()), []);
    // SF-013: the dApp's own desired-state revision builder, executed for real.
    const desired = loader()("lib/desired-state.ts"), group = groups[0].objectId;
    stage = "desired revision"; await execute(desired.buildDesiredRevisionTx([group], { componentName: "test-app", version: "2.0.0",
      recipeBlobId: "fixture-recipe-2", artifactSha256: "ab".repeat(32), recipeSha256: "cd".repeat(32) }));
    let states = await desired.fetchDesiredStates(client, [group]);
    assert.equal(states[group].revision, 1); assert.deepEqual(Object.keys(states[group].components), ["test-app"]);
    const managed = states[group].components["test-app"];
    deployments = await sui.fetchDeploymentEvents(client, operator.toSuiAddress());
    assert.deepEqual(deployments.map((d) => [d.deploymentId, d.version, d.targetGroups, d.targetDevices]), [[managed, "2.0.0", [group], []]]);
    stage = "desired removal"; await execute(desired.buildDesiredRevisionTx([group], undefined, ["test-app"]));
    states = await desired.fetchDesiredStates(client, [group]);
    assert.equal(states[group].revision, 2); assert.deepEqual(states[group].components, {});
    stage = "delete managed deployment"; await execute(sui.buildDeleteDeploymentTx(managed));
    assert.deepEqual(await sui.fetchDeploymentEvents(client, operator.toSuiAddress()), []);
    if (process.env.CATALOG_TEST === "1") {
      const catalog = loader()("lib/catalog.ts"), selector = loader()("lib/component-selector.ts");
      stage = "catalog create"; await execute(catalog.buildCreateCatalogTx());
      const catalogs = await catalog.fetchCatalogs(client, operator.toSuiAddress());
      assert.equal(catalogs.length, 1);
      const manifests = [
        { os:"linux", arch:"x86_64", recipeBlobId:"x86-recipe", recipeSha256:"ab".repeat(32), artifactSha256:"cd".repeat(32) },
        { os:"linux", arch:"aarch64", recipeBlobId:"arm-recipe", recipeSha256:"ef".repeat(32), artifactSha256:"01".repeat(32) },
      ];
      for (const version of ["1.0.0", "2.0.0-rc.1+build.1", "2.0.0"]) {
        stage = "catalog register"; await execute(catalog.buildPublishVersionTx(catalogs[0].objectId,"app",version,manifests));
      }
      stage = "catalog discovery";
      const versions = await catalog.discoverVersions(client,catalogs[0].objectId);
      assert.deepEqual(versions.map(v=>v.version),["2.0.0","2.0.0-rc.1+build.1","1.0.0"]);
      for(const v of versions) {
        assert.equal(selector.selectManifest(v.name,v.version,v.manifests,"linux","x86_64").recipeBlobId,"x86-recipe");
        assert.equal(selector.selectManifest(v.name,v.version,v.manifests,"linux","aarch64").recipeBlobId,"arm-recipe");
      }
      stage = "catalog mixed group deployment";
      await execute(catalog.buildCatalogDeploymentTx(versions[0].objectId,[groups[0].objectId],[devices[0].objectId,foreignDevice.objectId]));
      const catalogDeployments=await sui.fetchDeploymentEvents(client,operator.toSuiAddress());
      assert.equal(catalogDeployments.length,1); assert.equal(catalogDeployments[0].recipeBlobId,"");
      await execute(sui.buildDeleteDeploymentTx(catalogDeployments[0].deploymentId,catalogDeployments[0].schemaVersion));
      assert.deepEqual(await sui.fetchDeploymentEvents(client,operator.toSuiAddress()),[]);
      console.log("PASS: generated SDK catalog registration, immutable/type-authenticated discovery and SemVer sorting, both platform selectors, mixed targets and schema2 cleanup");
    }
    console.log("PASS: real gRPC publish/register+activate/borrowed group add and cap reissue/deployment/receipt/deregister/delete, invalid and foreign target history, failed execution effects");
    console.log("PASS: dApp desired-state revision (create+commit in one transaction), removal revision and managed-record deletion on chain");
    console.log(JSON.stringify({ chain: manifest.chain_identifier, package: packageId, registry: registryId }));
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
})().catch((error) => { console.error(`${stage}: ${error.message}`); process.exitCode = 1; });
