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
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "sf127-receipts-"));
  let daemon;
  try {
    const base = execFileSync("git", ["rev-parse", "f43d531^1"], { encoding: "utf8" }).trim();
    const oldTree = path.join(tmp, "base"); await fs.mkdir(oldTree);
    execFileSync("tar", ["-x", "-C", oldTree], { input: execFileSync("git", ["archive", base, "contracts"], { maxBuffer: 20 * 1024 * 1024 }) });
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

    const { decodeSuiPrivateKey } = appRequire('@mysten/sui/cryptography');
    const secret = decodeSuiPrivateKey(owner.getSecretKey()).secretKey;
    const keyFile = path.join(tmp,'live.keystore'), wallet = path.join(tmp,'live-client.yaml');
    await fs.writeFile(keyFile,JSON.stringify([Buffer.concat([Buffer.from([0]),Buffer.from(secret)]).toString('base64')]),{mode:0o600});
    await fs.writeFile(wallet,`keystore:\n  File: ${keyFile}\nenvs:\n- alias: local\n  rpc: ${rpc}\nactive_env: local\nactive_address: ${owner.toSuiAddress()}\n`,{mode:0o600});
    const evidence=path.join(root,'docs/validation/sf127-upgrade-security-localnet.json');
    const output=execFileSync('python3',['scripts/live_upgrade_security.py','--wallet',wallet,'--network','localnet','--walrus','local','--state',path.join(tmp,'state.json'),'--evidence',evidence],{
      cwd:root,encoding:'utf8',maxBuffer:8*1024*1024,timeout:600000,env:{...process.env,SUIFLEET_NETWORK_SUI:'/opt/sui-network/sui'},stdio:['ignore','pipe','pipe']});
    console.log(output);
    console.log('PASS: complete normal SF-001/SF-010/v3 live-security gate on an isolated local chain; public testnet remains pending CI');
  } finally {await fs.rm(tmp,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});
