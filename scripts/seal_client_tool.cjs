// Test helper for scripts/test_seal_localnet.py and live Seal checks: runs
// the dapp's own encryption module (dapp/src/lib/seal.ts, real @mysten/seal
// SDK against a real chain) and, for account-scope checks, the official
// SDK's decryption with an account's own key.
//
// stdin JSON, stdout JSON:
//   {"op":"encrypt","manifest":{...},"servers":[{objectId,weight}],"threshold":2,
//    "policyId":"0x..","keyEpoch":"0","namespace":"0x.. (optional override)",
//    "identity":"0x.. (optional, reuse)","items":[{"kind":"artifact","hex":".."}]}
//    -> {"identity":"0x..","envelopes":["hex",..],"sha256":["hex",..]}
//   {"op":"address","manifest":{...},"servers":[..],"secretKey":"hex seed"} -> {"address":"0x.."}
//   {"op":"decrypt","manifest":{...},"servers":[..],"secretKey":"suiprivkey..|hex",
//    "policyId":"0x..","envelope":"hex","function":"seal_approve_account"}
//    -> {"plaintext":"hex"} or {"error":"..","name":"NoAccessError"}
const crypto = require("node:crypto");
const { loader, appRequire } = require("./frontend_test_utils.cjs");

(async () => {
  const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(input.manifest);
  process.env.NEXT_PUBLIC_SEAL_KEY_SERVERS = JSON.stringify(input.servers);
  process.env.NEXT_PUBLIC_SEAL_THRESHOLD = String(input.threshold ?? 1);
  const load = loader();
  const seal = load("lib/seal.ts");
  const access = load("lib/access-policy.ts");
  const { SuiGrpcClient } = appRequire("@mysten/sui/grpc");
  const client = new SuiGrpcClient({ network: input.manifest.network, baseUrl: input.manifest.rpc_url });
  if (input.op === "encrypt") {
    const namespace = input.namespace ?? access.sealNamespace();
    const identity = input.identity ?? seal.newIdentity(input.policyId, input.keyEpoch);
    const encrypt = seal.sealer(client, seal.SEAL_SETTINGS, namespace, identity, true);
    const envelopes = [], digests = [];
    for (const item of input.items) {
      const plaintext = Buffer.from(item.hex, "hex");
      const sha256 = crypto.createHash("sha256").update(plaintext).digest("hex");
      envelopes.push(Buffer.from(await encrypt(new Uint8Array(plaintext), item.kind, sha256)).toString("hex"));
      digests.push(sha256);
    }
    console.log(JSON.stringify({ identity, envelopes, sha256: digests }));
    return;
  }
  if (input.op === "address") {
    const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
    console.log(JSON.stringify({ address: Ed25519Keypair.fromSecretKey(Buffer.from(input.secretKey, "hex")).toSuiAddress() }));
    return;
  }
  if (input.op === "decrypt") {
    const { SealClient, SessionKey, EncryptedObject } = appRequire("@mysten/seal");
    const { Ed25519Keypair } = appRequire("@mysten/sui/keypairs/ed25519");
    const { Transaction } = appRequire("@mysten/sui/transactions");
    const keypair = input.secretKey.startsWith("suiprivkey") ? Ed25519Keypair.fromSecretKey(input.secretKey)
      : Ed25519Keypair.fromSecretKey(Buffer.from(input.secretKey, "hex"));
    const envelope = Buffer.from(input.envelope, "hex");
    const object = new Uint8Array(envelope.subarray(seal.MAGIC.length));
    const parsed = EncryptedObject.parse(object);
    const sealClient = new SealClient({ suiClient: client, serverConfigs: input.servers, verifyKeyServers: true });
    const session = await SessionKey.create({ address: keypair.toSuiAddress(), packageId: access.sealNamespace(), ttlMin: 10, signer: keypair, suiClient: client });
    const tx = new Transaction();
    tx.moveCall({ target: `${input.manifest.call_package_id}::seal_policy::${input.function ?? "seal_approve_account"}`,
      arguments: [tx.pure.vector("u8", Buffer.from(parsed.id, "hex")), tx.object(input.policyId)] });
    const txBytes = await tx.build({ client, onlyTransactionKind: true });
    try {
      const plaintext = await sealClient.decrypt({ data: object, sessionKey: session, txBytes, checkShareConsistency: true });
      console.log(JSON.stringify({ plaintext: Buffer.from(plaintext).toString("hex") }));
    } catch (error) {
      console.log(JSON.stringify({ error: String(error?.message ?? error), name: error?.constructor?.name }));
    }
    return;
  }
  throw new Error(`unknown op ${input.op}`);
})().catch(error => { console.error(error); process.exit(1); });
