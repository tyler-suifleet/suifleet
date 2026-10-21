#!/usr/bin/env node
// Writes interfaces/seal-vectors.json: Seal interoperability vectors produced
// by the pinned @mysten/seal SDK (dapp lockfile), which the edge's independent
// Rust implementation must reproduce (suifleet-edge/src/seal.rs tests).
//
// Test-only master keys; nothing here is a real key server's secret.
//   node scripts/generate_seal_vectors.mjs
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const modules = resolve(root, "dapp/node_modules/@mysten");
const load = (path) => import(pathToFileURL(resolve(modules, path)).href);
const { G1Element, G2Element, Scalar } = await load("seal/dist/bls12381.mjs");
const { hashToG1 } = await load("seal/dist/kdf.mjs");
const { DST_POP } = await load("seal/dist/ibe.mjs");
const { KeyServerMove, KeyServerMoveV1, EncryptedObject } = await load("seal/dist/bcs.mjs");
const { SealClient, SessionKey, DemType } = await load("seal/dist/index.mjs");
const { Ed25519Keypair } = await load("sui/dist/keypairs/ed25519/index.mjs");

const sealVersion = JSON.parse(readFileSync(resolve(modules, "seal/package.json"), "utf8")).version;
const hex = (bytes) => Buffer.from(bytes).toString("hex");
const unhex = (text) => Uint8Array.from(Buffer.from(text.replace(/^0x/, ""), "hex"));
// Deterministic test scalars, below the group order.
const scalar = (label) => Scalar.fromBytes(Uint8Array.from(createHash("sha256").update(label).digest().map((b, i) => (i === 0 ? b & 0x3f : b))));
const objectId = (label) => "0x" + createHash("sha256").update(label).digest("hex");

const packageId = objectId("package");
const policy = objectId("policy");
const epoch = Buffer.alloc(8); epoch.writeBigUInt64LE(3n);
const innerId = "0x" + hex(Buffer.concat([unhex(policy), epoch, Buffer.from("00112233445566778899aabbccddeeff", "hex")]));
const fullId = Buffer.concat([unhex(packageId), unhex(innerId)]);

const servers = ["a", "b", "c"].map((name) => {
  const secret = scalar(`server-${name}`);
  return { name, objectId: objectId(`server-${name}`), secret, pk: G2Element.generator().multiply(secret).toBytes() };
});

// A stand-in chain client: key server objects and a first-version package.
const suiClient = {
  core: {
    async getObject({ objectId: id }) {
      if (id === packageId) return { object: { version: "1" } };
      const server = servers.find((s) => s.objectId === id);
      return { object: { content: KeyServerMove.serialize({ id, firstVersion: 1, lastVersion: 1 }).toBytes(), version: "7" }, server };
    },
    async getDynamicField({ parentId }) {
      const server = servers.find((s) => s.objectId === parentId);
      const value = KeyServerMoveV1.serialize({ name: server.name, url: `https://${server.name}.invalid`, keyType: 0, pk: server.pk }).toBytes();
      return { dynamicField: { value: { bcs: value } } };
    },
  },
};
const client = new SealClient({ suiClient, serverConfigs: servers.map((s) => ({ objectId: s.objectId, weight: 1 })) });

const plaintext = Buffer.from("SuiFleet confidential artifact bytes: not for public Walrus readers.\n".repeat(3));
const aad = Buffer.from("suifleet/seal/v1/artifact/" + createHash("sha256").update(plaintext).digest("hex"));
const encrypted = [];
for (const [dem, demType] of [["aes256gcm", DemType.AesGcm256], ["hmac256ctr", DemType.Hmac256Ctr]]) {
  const { encryptedObject, key } = await client.encrypt({ threshold: 2, packageId, id: innerId, data: plaintext, aad, demType });
  const parsed = EncryptedObject.parse(encryptedObject);
  encrypted.push({ dem, encrypted_object: hex(encryptedObject), dem_key: hex(key), threshold: parsed.threshold,
    services: parsed.services.map(([id, index]) => ({ object_id: id, index })) });
}

const gid = hashToG1(fullId);
const elgamalSecret = scalar("elgamal");
const randomness = scalar("elgamal-r");
const userKey = gid.multiply(servers[0].secret);
const g = G1Element.generator();
const popMessage = Buffer.concat([DST_POP, servers[0].pk, unhex(servers[0].objectId)]);

const seed = createHash("sha256").update("device").digest();
const device = Ed25519Keypair.fromSecretKey(seed);
const message = new TextEncoder().encode("Accessing keys of package " + packageId + " for 10 mins from 2026-10-16 12:00:00 UTC, session key AAAA");
const { signature: personalSignature } = await device.signPersonalMessage(message);

// The SDK's own session: certificate message and request signature.
const sessionSeed = createHash("sha256").update("session").digest();
const creationTimeMs = Date.now();
const session = SessionKey.import({ address: device.toSuiAddress(), packageId, ttlMin: 10, creationTimeMs,
  sessionKey: Ed25519Keypair.fromSecretKey(sessionSeed).getSecretKey(), personalMessageSignature: undefined }, suiClient);
const ptbKind = Uint8Array.from([0, 1, 2, 3, 4, 5]);
const request = await session.createRequestParams(ptbKind);

const vectors = {
  generator: `@mysten/seal ${sealVersion} via scripts/generate_seal_vectors.mjs; test-only master keys`,
  package_id: packageId,
  inner_id: innerId,
  full_id: hex(fullId),
  servers: servers.map((s) => ({ object_id: s.objectId, master_secret: hex(s.secret.toBytes()), public_key: hex(s.pk),
    user_secret_key: hex(gid.multiply(s.secret).toBytes()) })),
  plaintext: hex(plaintext),
  aad: hex(aad),
  encrypted,
  hash_to_g1: hex(gid.toBytes()),
  gt: { g1_scalar: hex(scalar("gt-a").toBytes()), g2_scalar: hex(scalar("gt-b").toBytes()),
    bytes: hex(g.multiply(scalar("gt-a")).pairing(G2Element.generator().multiply(scalar("gt-b"))).toBytes()) },
  elgamal: { secret: hex(elgamalSecret.toBytes()), public_key: hex(g.multiply(elgamalSecret).toBytes()),
    verification_key: hex(G2Element.generator().multiply(elgamalSecret).toBytes()),
    ciphertext: [hex(g.multiply(randomness).toBytes()), hex(userKey.add(g.multiply(elgamalSecret).multiply(randomness)).toBytes())],
    plaintext: hex(userKey.toBytes()) },
  proof_of_possession: { object_id: servers[0].objectId, public_key: hex(servers[0].pk),
    signature: hex(G1Element.hashToCurve(popMessage).multiply(servers[0].secret).toBytes()) },
  personal_message: { seed: hex(seed), address: device.toSuiAddress(), message: hex(message), signature: personalSignature },
  session: { seed: hex(sessionSeed), package_id: packageId, ttl_min: 10, creation_time_ms: creationTimeMs,
    message: new TextDecoder().decode(session.getPersonalMessage()), ptb: hex(ptbKind.slice(1)),
    enc_key: hex(request.encKeyPk), enc_verification_key: hex(request.encVerificationKey),
    request_signature: Buffer.from(request.requestSignature, "base64").toString("hex") },
};
writeFileSync(resolve(root, "interfaces/seal-vectors.json"), JSON.stringify(vectors, null, 2) + "\n");
console.log(`PASS: wrote interfaces/seal-vectors.json from @mysten/seal ${sealVersion}`);
