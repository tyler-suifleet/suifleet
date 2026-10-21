// Offline cost specimen: real app Seal envelopes with test-vector public keys.
// No provider access, wallet, upload or transaction. Never deploy these fixtures.
// node scripts/cost_workload.cjs OUTPUT_DIRECTORY
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { loader } = require('./frontend_test_utils.cjs');

(async () => {
  for (const [name, expected] of [['seal', '1.5.0'], ['sui', '2.36.0']]) {
    const version = JSON.parse(fs.readFileSync(path.resolve(__dirname, `../dapp/node_modules/@mysten/${name}/package.json`))).version;
    if (version !== expected) throw new Error(`expected app ${name} ${expected}, got ${version}`);
  }
  const vectors = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../interfaces/seal-vectors.json')));
  const { KeyServerMove, KeyServerMoveV1 } = await import(require('node:url').pathToFileURL(
    path.resolve(__dirname, '../dapp/node_modules/@mysten/seal/dist/bcs.mjs')));
  const { sealer } = loader()('lib/seal.ts');
  const servers = vectors.servers.map((s) => ({ objectId: s.object_id, pk: Buffer.from(s.public_key, 'hex') }));
  const client = { core: {
    async getObject({ objectId }) {
      if (objectId === vectors.package_id) return { object: { version: '1' } };
      if (!servers.some((s) => s.objectId === objectId)) throw new Error('unknown test server');
      return { object: { content: KeyServerMove.serialize({ id: objectId, firstVersion: 1, lastVersion: 1 }).toBytes(), version: '1' } };
    },
    async getDynamicField({ parentId }) {
      const server = servers.find((s) => s.objectId === parentId);
      return { dynamicField: { value: { bcs: KeyServerMoveV1.serialize({ name: 'offline-cost-fixture', url: 'https://cost.invalid', keyType: 0, pk: server.pk }).toBytes() } } };
    },
  } };
  const encrypt = sealer(client, { keyServers: servers.map((s) => ({ objectId: s.objectId, weight: 1 })), threshold: 2 }, vectors.package_id, vectors.inner_id, false);
  const artifact = Buffer.alloc(1048576, 65);
  const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
  const document = { schemaVersion: '1.0', componentName: 'cost-specimen', componentVersion: '1.0.0',
    artifactBlobId: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', artifactSha256: sha(artifact),
    lifecycle: { install: 'true' }, configuration: {} };
  loader()('lib/recipe-schema.ts').assertComponentRecipe(document);
  const recipe = Buffer.from(JSON.stringify(document, null, 2));
  const output = path.resolve(process.argv[2]);
  fs.mkdirSync(output, { recursive: true });
  const blobs = [];
  for (const [kind, plain] of [['artifact', artifact], ['recipe', recipe]]) {
    const bytes = Buffer.from(await encrypt(plain, kind, sha(plain)));
    fs.writeFileSync(path.join(output, `${kind}.seal`), bytes);
    blobs.push({ kind, plaintext_bytes: plain.length, plaintext_sha256: sha(plain),
      ciphertext_bytes: bytes.length, ciphertext_sha256: sha(bytes) });
  }
  console.log(JSON.stringify({ kind: 'offline_encryption_specimen', seal: '@mysten/seal 1.5.0',
    key_servers: 3, threshold: 2, blobs,
    limits: 'Synthetic content, test public keys; no upload, purchase, real expiry or end-to-end deployment measured.' }, null, 2));
})().catch((e) => { console.error(e); process.exitCode = 1; });
