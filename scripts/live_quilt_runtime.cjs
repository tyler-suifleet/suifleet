// Dedicated-testnet acceptance helper. No keys enter Node. The parent signs
// bounded personal messages through the explicit test-wallet CLI, never PTBs.
// Public providers/test fixtures establish no production-provider acceptance.
const fs = require('node:fs'), path = require('node:path');
const {createHash, randomBytes} = require('node:crypto');
const {appRequire, loader} = require('./frontend_test_utils.cjs');
const input = JSON.parse(fs.readFileSync(process.argv[2]));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(input.manifest);
const load = loader(), bulk = load('lib/fleet-bulk.ts'), data = load('lib/fleet-data.ts');
const {SuiGrpcClient} = appRequire('@mysten/sui/grpc');
const {SealClient, SessionKey} = appRequire('@mysten/seal');
const {bcs} = appRequire('@mysten/sui/bcs');
const {normalizeSuiAddress:id} = appRequire('@mysten/sui/utils');
const {isValidPersonalMessageSignature} = appRequire('@mysten/sui/verify');
const {walrus, TESTNET_WALRUS_PACKAGE_CONFIG} = appRequire('@mysten/walrus');
const lines = require('node:readline').createInterface({input:process.stdin})[Symbol.asyncIterator]();
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
async function sign(message, address) {
  console.log(JSON.stringify({personal_message:Buffer.from(message).toString('base64')}));
  const response = await lines.next();
  if (response.done) throw Error('Signature bridge unavailable');
  const {signature} = JSON.parse(response.value);
  if (!await isValidPersonalMessageSignature(message, signature, {address})) throw Error('Personal signature mismatch');
  return signature;
}
let stage = 'initialization';
async function main() {
  if (input.config.chainIdentifier !== '4c78adac') throw Error('Testnet only');
  const client = new SuiGrpcClient({network:'testnet',baseUrl:input.manifest.rpc_url});
  const fleet = await data.readFleetData(client,input.config);
  const writer = await data.readDeviceData(client,input.config,input.device);
  if (!writer) throw Error('Writer unavailable');
  const servers = input.servers.map(objectId=>({objectId:id(objectId),weight:1}));
  if (input.operation === 'prepare') {
    // This fixture has only public synthetic lines, never customer data.
    const seq = Number(writer.batch_seq)+1, observed_ms = Date.now();
    const base = {schema:1,kind:'fleet-data',chain_identifier:input.config.chainIdentifier,registry:input.config.registryId,
      fleet_id:input.config.fleetId,device_id:id(input.device),signer:id(input.signer),seq,observed_ms,
      reason:'logs',dropped_lines:0,records:[]};
    const seal = new SealClient({suiClient:client,serverConfigs:servers,verifyKeyServers:true});
    const items = [];
    async function encrypt(archive) {
      const message = Buffer.from(JSON.stringify(archive));
      const signature = await sign(message,input.signer);
      const plaintext = Buffer.from(JSON.stringify({message:message.toString('base64'),signature}));
      const identity = Buffer.concat([Buffer.from(bcs.Address.serialize(input.config.fleetId).toBytes()),
        Buffer.from(bcs.u64().serialize(fleet.key_epoch).toBytes()),randomBytes(16)]);
      const {encryptedObject} = await seal.encrypt({threshold:2,packageId:input.config.typeOrigin,id:identity.toString('hex'),
        data:plaintext,aad:bulk.archiveAad(input.config,{seq:String(seq),kind:0})});
      plaintext.fill(0);
      return Buffer.concat([Buffer.from('suifleet/seal/v1\n'),Buffer.from(encryptedObject)]);
    }
    for (const [n,source] of ['daemon','component:sensor'].entries()) {
      const bytes = await encrypt({...base,records:[{source,line:`public synthetic Quilt item ${n}`}]});
      const identifier = `item-${String(n).padStart(2,'0')}`, file = path.join(input.directory,identifier+'.bin');
      fs.writeFileSync(file,bytes,{mode:0o600});items.push({identifier,size:bytes.length,sha256:sha(bytes),path:file});
    }
    const manifest = {...base,schema:4,...(input.config.compactHistoryOrigin ? {previous_head:[]} : {}),items:items.map(({path,...item})=>item)};
    const bytes = await encrypt(manifest);
    fs.writeFileSync(path.join(input.directory,'ciphertext.bin'),bytes,{mode:0o600});
    console.log(JSON.stringify({prepared:{items,seq,kind:0,ciphertext_sha256:sha(bytes),
      ciphertext_bytes:bytes.length+items.reduce((n,i)=>n+i.size,0),plaintext_bytes:Buffer.byteLength(JSON.stringify(manifest))}}));
  } else if (['read','read-denied'].includes(input.operation)) {
    const session = await SessionKey.create({address:input.signer,packageId:input.config.typeOrigin,ttlMin:5,suiClient:client});
    await session.setPersonalMessageSignature(await sign(session.getPersonalMessage(),input.signer));
    const storage = client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
    const ownerDecrypt = await bulk.archiveDecryptor(client,input.config,{keyServers:servers,threshold:2},session,
      {kind:'owner',ownerCapId:input.budgetCap});
    const plaintextSizes = [];
    const decrypt = async encrypted => {
      const bytes = await ownerDecrypt(encrypted);
      plaintextSizes.push({envelope_bytes:bytes.length,message_bytes:Buffer.from(JSON.parse(Buffer.from(bytes).toString()).message,'base64').length});
      return bytes;
    };
    stage = 'owner-read';
    const archive = await bulk.fetchVerifiedArchive(client,storage,input.config,input.device,0,input.manifest.walrus.aggregator_url,decrypt);
    if (archive.schema !== 4 || archive.items.length !== 2 || archive.records.length !== 2) throw Error('Wrong Quilt archive');
    const support = await bulk.archiveDecryptor(client,input.config,{keyServers:servers,threshold:2},session,{kind:'support'});
    stage = 'support-read';
    let supportDenied = false;
    try {
      await bulk.fetchVerifiedArchive(client,storage,input.config,input.device,0,input.manifest.walrus.aggregator_url,support);
    } catch (error) {
      const {NoAccessError} = appRequire('@mysten/seal');
      if (input.operation !== 'read-denied' || !(error instanceof NoAccessError)) throw error;
      supportDenied = true;
    }
    if (supportDenied !== (input.operation === 'read-denied')) throw Error('Support authority transition failed');
    const {readVerifiedItem} = await import('../dapp/scripts/archive-quilt.mjs');
    const {blobIdFromInt} = appRequire('@mysten/walrus');
    const [anchor] = await bulk.readAnchors(client,input.config,input.device,0,1);
    const parent = blobIdFromInt(anchor.blob_id), expected = archive.items[0];
    stage = 'bounded-parent';
    const parentBytes = await bulk.fetchArchiveBytes(input.manifest.walrus.aggregator_url.replace(/\/+$/,'')+'/v1/blobs/'+parent);
    // Known authenticated fixture: independent actual SDK patch roundtrip.
    const {readArchiveQuilt} = load('lib/archive-quilt.ts');
    const bounded = readArchiveQuilt(parentBytes);
    stage = 'sdk-patch-interoperability';
    for(const file of await (await storage.getBlob({blobId:parent})).files()) {
      const name = await file.getIdentifier(), actual = await file.bytes();
      if (!bounded.has(name) || sha(actual) !== sha(bounded.get(name))) throw Error('SDK Quilt patch differs from bounded parent');
    }
    stage = 'negative-patch-controls';
    for (const bad of [{...expected,sha256:'00'.repeat(32)},{...expected,identifier:'missing'}]) {
      let refused = false;
      try { await readVerifiedItem(parentBytes,bad); }
      catch (error) { if (!/digest mismatch|Missing or ambiguous/.test(error.message)) throw error; refused = true; }
      if (!refused) throw Error('Substituted patch accepted');
    }
    console.log(JSON.stringify({result:{verified:true,owner:true,support:!supportDenied,supportDenied,wrongOrMissingPatchRefused:true,plaintextSizes,compression:'none',items:archive.items.length,records:archive.records.length,seq:archive.seq}}));
  } else throw Error('Unknown operation');
}
main().then(()=>process.exit(0)).catch(error=>{console.error(JSON.stringify({stage,errorType:error.constructor.name}));process.exit(1)});
