// Confidential delivery in the frontend (SF-022), offline: Seal settings
// carry no credentials, identities have the policy layout, uploads are
// encrypted before any request and never fall back to plaintext, and the
// envelope is what devices expect. The real @mysten/seal SDK encrypts.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { loader, appRequire } = require("./frontend_test_utils.cjs");

const root = path.resolve(__dirname, "..");
const protocol = JSON.parse(fs.readFileSync(path.join(root, "interfaces/fleet-v1.json"), "utf8"));
const vectors = JSON.parse(fs.readFileSync(path.join(root, "interfaces/seal-vectors.json"), "utf8"));
const fixture = JSON.parse(fs.readFileSync(path.join(root, "manifests/fixture.localnet.json"), "utf8"));
for (const name of protocol.optional_type_origins.seal_policy) fixture.type_origins[name] = fixture.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const servers = vectors.servers.map(s => ({ objectId: s.object_id, weight: 1 }));
process.env.NEXT_PUBLIC_SEAL_KEY_SERVERS = JSON.stringify(servers);
process.env.NEXT_PUBLIC_SEAL_THRESHOLD = "2";

const load = loader();
const seal = load("lib/seal.ts");
const walrus = load("lib/walrus.ts");
const delivery = load("lib/delivery.ts");
const access = load("lib/access-policy.ts");
const { EncryptedObject } = appRequire("@mysten/seal");
const { bcs } = appRequire("@mysten/sui/bcs");

// Key server objects as the chain returns them (the SDK reads these).
const KeyServer = bcs.struct("KeyServer", { id: bcs.Address, firstVersion: bcs.u64(), lastVersion: bcs.u64() });
const KeyServerV1 = bcs.struct("KeyServerV1", { name: bcs.string(), url: bcs.string(), keyType: bcs.u8(), pk: bcs.byteVector() });
const namespace = access.sealNamespace();
const chain = {
  core: {
    async getObject({ objectId }) {
      if (objectId === namespace) return { object: { version: "1" } };
      return { object: { content: KeyServer.serialize({ id: objectId, firstVersion: 1, lastVersion: 1 }).toBytes() } };
    },
    async getDynamicField({ parentId }) {
      const server = vectors.servers.find(s => s.object_id === parentId);
      return { dynamicField: { value: { bcs: KeyServerV1.serialize({ name: "test", url: "https://seal.invalid", keyType: 0, pk: Buffer.from(server.public_key, "hex") }).toBytes() } } };
    },
  },
};

(async () => {
  // Settings: public IDs only; a provider credential is refused outright.
  assert.equal(seal.SEAL_SETTINGS.threshold, 2);
  assert.throws(() => seal.parseSealSettings(JSON.stringify([{ objectId: "0x1", weight: 1, apiKey: "secret" }]), "1"), /credentials/);
  assert.throws(() => seal.parseSealSettings(JSON.stringify([{ objectId: "0x1" }, { objectId: "0x01" }]), "1"), /duplicate/);
  assert.throws(() => seal.parseSealSettings(JSON.stringify([{ objectId: "0x1" }]), "2"), /threshold/);
  assert.equal(seal.parseSealSettings(undefined, undefined), undefined);

  // Identity: policy ID || u64 LE epoch || 32 random bytes, fresh each time.
  const policyId = "0x" + "ab".repeat(32);
  const identity = seal.newIdentity(policyId, 258n);
  assert.equal(identity.length, 2 + 2 * (32 + 8 + 32));
  assert.equal(identity.slice(2, 66), "ab".repeat(32));
  assert.equal(identity.slice(66, 82), "0201000000000000");
  assert.notEqual(seal.newIdentity(policyId, 258n), identity);

  // Uploads: count every request the frontend makes.
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url: String(url), body: new Uint8Array(await new Response(init.body).arrayBuffer()) });
    return new Response(JSON.stringify({ newlyCreated: { blobObject: { id: "0x9", blobId: `blob${requests.length}`, storage: { endEpoch: 9 } }, cost: 1 } }));
  };
  const plaintext = new TextEncoder().encode("proprietary firmware image ".repeat(20));
  const file = { arrayBuffer: async () => plaintext.buffer.slice(0) };

  // Confidential: encrypted with the real SDK before the only request.
  const protection = { kind: "confidential", seal: seal.sealer(chain, seal.SEAL_SETTINGS, namespace, identity, false) };
  const uploaded = await walrus.uploadArtifact(file, "0x5", protection);
  assert.equal(uploaded.confidential, true);
  assert.equal(requests.length, 1);
  const body = requests[0].body;
  assert.ok(seal.isEnvelope(body));
  assert.ok(!Buffer.from(body).includes(Buffer.from(plaintext.slice(0, 32))), "plaintext reached the publisher");
  const object = EncryptedObject.parse(body.slice(seal.MAGIC.length));
  assert.equal(object.id, identity.slice(2));
  assert.equal(object.threshold, 2);
  assert.equal(Buffer.from(object.ciphertext.Aes256Gcm.aad).toString(), `suifleet/seal/v1/artifact/${uploaded.sha256}`);
  assert.equal(require("node:crypto").createHash("sha256").update(plaintext).digest("hex"), uploaded.sha256, "the plaintext digest is what deployments record");

  // Recipes too; the same release identity.
  const recipe = { schemaVersion: "1.0", componentName: "app", componentVersion: "1.0.0", artifactBlobId: uploaded.blobId, artifactSha256: uploaded.sha256, lifecycle: {}, configuration: {} };
  const recipeUpload = await walrus.uploadRecipe(recipe, "0x5", protection);
  const recipeObject = EncryptedObject.parse(requests[1].body.slice(seal.MAGIC.length));
  assert.equal(recipeObject.id, identity.slice(2));
  assert.equal(Buffer.from(recipeObject.ciphertext.Aes256Gcm.aad).toString(), `suifleet/seal/v1/recipe/${recipeUpload.sha256}`);

  // Encryption failure or a non-envelope result: nothing is uploaded, ever.
  const before = requests.length;
  await assert.rejects(walrus.uploadArtifact(file, "0x5", { kind: "confidential", seal: async () => { throw new Error("key servers unreachable"); } }), /unreachable/);
  await assert.rejects(walrus.uploadArtifact(file, "0x5", { kind: "confidential", seal: async () => plaintext }), /nothing was uploaded/);
  await assert.rejects(walrus.uploadArtifact(file, "0x5", {}), /confidential or explicitly public/);
  assert.equal(requests.length, before, "a failed encryption must not upload");

  // Release protection: confidential by default; public only when acknowledged.
  const policy = { objectId: policyId, capId: "0x7", name: "p", version: "1", keyEpoch: "4", accounts: [], devices: [], groups: [] };
  assert.equal(delivery.DEFAULT_DELIVERY.mode, "confidential");
  assert.throws(() => delivery.releaseProtection(chain, { ...delivery.DEFAULT_DELIVERY }, [policy]), /access policy/);
  assert.throws(() => delivery.releaseProtection(chain, { mode: "confidential", policyId, publicAcknowledged: true }, [policy], null), /key servers/);
  assert.throws(() => delivery.releaseProtection(chain, { mode: "public", policyId: "", publicAcknowledged: false }, [policy]), /Confirm/);
  assert.equal(delivery.releaseProtection(chain, { mode: "public", policyId: "", publicAcknowledged: true }, []).kind, "public");
  assert.equal(delivery.releaseProtection(chain, { mode: "confidential", policyId, publicAcknowledged: false }, [policy]).kind, "confidential");

  // Explicitly public: the plaintext itself.
  const publicUpload = await walrus.uploadArtifact(file, "0x5", { kind: "public" });
  assert.equal(publicUpload.confidential, false);
  assert.deepEqual(requests.at(-1).body, plaintext);

  // Storage probes recognize a confidential recipe without parsing it.
  global.fetch = async () => new Response(requests[1].body);
  assert.deepEqual(await walrus.probeDeploymentStorage("blob2"), { recipe: "available", artifact: "unknown", confidential: true });

  // Policy transactions target the fleet package's seal_policy module.
  const grant = access.buildGrantTx({ ...policy, objectId: "0x" + "11".repeat(32) }, "device", "0x22");
  const data = grant.getData();
  assert.equal(data.commands[0].MoveCall.module, "seal_policy");
  assert.equal(data.commands[0].MoveCall.function, "grant_device");
  assert.equal(access.buildRotateTx(policy).getData().commands[0].MoveCall.function, "rotate_key_epoch");
  assert.throws(() => access.buildCreatePolicyTx("  ", "0x5"), /name/);
  console.log("PASS: confidential uploads encrypt before any request, never fall back to plaintext, carry no credentials and bind kind/digest");
})().catch(error => { console.error(error); process.exit(1); });
