// Dedicated-wallet testnet acceptance. No keys: Python verifies/signs each exact
// transaction under testnet_budget and the machine-session mutation lock.
const fs = require('node:fs');
const readline = require('node:readline');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
const { fromBase58, toHex, toBase64, normalizeSuiAddress } = appRequire('@mysten/sui/utils');
const [mode, rpc, sender, gas] = process.argv.slice(2);
const client = new SuiGrpcClient({ network: 'testnet', baseUrl: rpc });
const load = loader();
const storage = load('lib/release-storage.ts');
async function facts() {
  const [{ response: service }, { response: epoch }] = await Promise.all([client.ledgerService.getServiceInfo({}), client.ledgerService.getEpoch({ readMask: { paths: ['epoch', 'system_state.protocol_version'] } })]);
  return { chain_identifier: toHex(fromBase58(service.chainId).slice(0, 4)), protocol_version: Number(epoch.epoch.systemState.protocolVersion), epoch: Number(service.epoch) };
}
(async () => {
  if (mode === 'facts') { await load('lib/deployment-manifest.ts').verifyDeployment(client); console.log(JSON.stringify(await facts())); return; }
  if (mode !== 'run') throw Error('Use facts or run');
  if ((await facts()).chain_identifier !== '4c78adac') throw Error('Testnet chain mismatch');
  const { verifyDeployment, requireManifest } = load('lib/deployment-manifest.ts');
  await verifyDeployment(client);
  const manifest = requireManifest();
  const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();
  const submit = async (tx, label, expected) => {
    await verifyDeployment(client);
    const history = JSON.parse(fs.readFileSync(process.env.SUIFLEET_RELEASE_STORAGE_EVIDENCE, 'utf8'));
    const prior = history.transactions.find(t => t.label === label && t.status === 'success');
    if (prior) {
      const confirmed = await client.waitForTransaction({ digest: prior.digest, include: { effects: true, events: true } });
      if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) throw Error('Prior transaction effects unavailable');
      return confirmed.Transaction;
    }
    tx.setSender(sender); tx.setGasBudget(BigInt(gas));
    console.log(JSON.stringify({ tx: toBase64(await tx.build({ client })), label, expected }));
    const reply = JSON.parse((await lines.next()).value);
    const confirmed = await client.waitForTransaction({ digest: reply.digest, include: { effects: true, events: true } });
    if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) throw Error('Transaction execution effects failed');
    return confirmed.Transaction;
  };
  const catalog = load('lib/catalog.ts'), walrus = load('lib/walrus.ts');
  const schedule = await storage.storageSchedule(client), days = 12, epochs = storage.periodEpochs(days, schedule);
  const uploaded = [];
  const store = async (body, progress) => { const result = await walrus.uploadBlob(body, 'application/octet-stream', sender, progress, epochs); uploaded.push(result); return result; };
  const statePath = process.env.SUIFLEET_RELEASE_STORAGE_STATE;
  const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8')) : { nonce: crypto.randomUUID() };
  const save = () => fs.writeFileSync(statePath, JSON.stringify(state, (_, v) => typeof v === 'bigint' ? v.toString() : v));
  save();
  const nonce = state.nonce;
  load('lib/recipe-schema.ts').assertComponentRecipe({ schemaVersion: '1.0', configuration: {}, componentName: `sf165-${nonce}`, componentVersion: '1.0.0', artifactBlobId: 'pending', artifactSha256: '0'.repeat(64), lifecycle: {} });
  const artifact = state.artifact ?? await walrus.uploadArtifact(new File([`SF165 acceptance ${nonce}\n`], 'sf165.bin'), sender, { kind: 'public' }, undefined, store);
  state.artifact = artifact; save();
  console.log(JSON.stringify({ upload: { ...artifact.storage, costFrost: artifact.storage.costFrost?.toString() } }));
  const recipe = state.recipe ?? await walrus.uploadRecipe({ schemaVersion: '1.0', configuration: {}, componentName: `sf165-${nonce}`, componentVersion: '1.0.0', artifactBlobId: artifact.blobId, artifactSha256: artifact.sha256, lifecycle: {} }, sender, { kind: 'public' }, undefined, store);
  state.recipe = recipe; save();
  console.log(JSON.stringify({ upload: { ...recipe.storage, costFrost: recipe.storage.costFrost?.toString() } }));
  if (uploaded.some(b => b.alreadyCertified || b.owner !== 'you' || !b.blobObjectId || b.endEpoch < schedule.epoch + epochs)) throw Error('Publisher did not create owner-renewable blobs for the chosen period');
  const created = await submit(catalog.buildCreateCatalogTx(), 'create disposable catalog', { package: manifest.call_package_id, module: 'component_catalog', function: 'create_catalog' });
  const { bcs } = appRequire('@mysten/sui/bcs');
  const event = created.events.find(e => e.eventType.endsWith('::component_catalog::CatalogCreated'));
  if (!event) throw Error('Catalog creation event missing');
  const catalogId = bcs.struct('CatalogCreated', { catalog_id: bcs.Address, publisher: bcs.Address }).parse(event.bcs).catalog_id;
  const published = await submit(catalog.buildPublishVersionTx(catalogId, `sf165-${nonce}`, '1.0.0', [{ os: 'linux', arch: 'x86_64', recipeBlobId: recipe.blobId, recipeSha256: recipe.sha256, artifactSha256: artifact.sha256 }]), 'publish chosen-period version', { package: manifest.call_package_id, module: 'component_catalog', function: 'publish_version' });
  const w = await storage.releaseWalrus(client);
  const before = await w.getBlobObject(artifact.storage.blobObjectId);
  const owned = await storage.releaseObjects(client, sender);
  if (!owned.some(o => o.id === before.id && o.blobId === artifact.blobId)) throw Error('Artifact ownership/blob binding not verified');
  const extra = storage.periodEpochs(2, schedule);
  const quote = await w.storageCost(Number(before.size), extra);
  const system = await w.systemObject();
  const extended = await submit(await storage.buildExtendWalletReleaseTx(client, before.id, extra), 'in-app wallet storage extension', { package: system.package_id, module: 'system', function: 'extend_blob', blob: before.id });
  const after = await (await storage.releaseWalrus(client)).getBlobObject(before.id);
  if (after.storage.end_epoch !== before.storage.end_epoch + extra) throw Error('Confirmed extension did not update expiry');
  const bytes = await (await storage.releaseWalrus(client)).readBlob({ blobId: artifact.blobId });
  if (new TextDecoder().decode(bytes) !== `SF165 acceptance ${nonce}\n`) throw Error('Artifact readback mismatch');
  const result = { days, epochs, schedule, catalogId, publicationDigest: published.digest, extensionDigest: extended.digest,
    beforeEndEpoch: before.storage.end_epoch, afterEndEpoch: after.storage.end_epoch, extensionQuoteFrost: quote.storageCost.toString(),
    uploads: uploaded.map(b => ({ ...b, costFrost: b.costFrost?.toString() })), artifactReadMatches: true,
    limits: ['Uses actual dApp upload, publish and extension builders with dedicated CLI signing; manual extension-wallet interaction is not claimed.', 'New immutable catalog version and non-deletable blobs retained until their finite expiry; no original app object changed.'] };
  console.log(JSON.stringify({ result }));
  lines.return?.();
})().catch(e => { console.error(String(e.stack ?? e)); process.exit(1); });
