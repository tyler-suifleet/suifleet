const assert = require('node:assert/strict'), fs=require('node:fs'), crypto=require('node:crypto');
const {loader,appRequire,manifestClient}=require('./frontend_test_utils.cjs');
const catalog=JSON.parse(fs.readFileSync('interfaces/fleet-v1.json')),m=JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
for(const names of [catalog.optional_type_origins.security_policy,catalog.optional_type_origins.customer,catalog.optional_type_origins.passkey_factor]) for(const n of names)m.type_origins[n]=m.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(m);
const load=loader(),p=load('lib/passkey-factor.ts'),wire=load('lib/fleet-protocol.ts');
const {bcs}=appRequire('@mysten/sui/bcs'),{PasskeyPublicKey}=appRequire('@mysten/sui/keypairs/passkey'),{ObjectError}=appRequire('@mysten/sui/client');
const C='0x'+'c'.padStart(64,'0'),A='0x'+'a'.padStart(64,'0');
const storage=new Map();global.localStorage={getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
global.location={origin:'https://console.example',hostname:'console.example'};
global.isSecureContext=true;global.PublicKeyCredential=function(){};
const pair=crypto.generateKeyPairSync('ec',{namedCurve:'prime256v1'});
const spki=pair.publicKey.export({type:'spki',format:'der'});
Object.defineProperty(global,'navigator',{configurable:true,value:{credentials:{
 create:async()=>({rawId:Buffer.from('fixture'),response:{getPublicKey:()=>spki}}),
 get:async({publicKey})=>{
  assert.equal(publicKey.userVerification,'required');assert.equal(publicKey.rpId,'console.example');
  assert.equal(Buffer.from(publicKey.allowCredentials[0].id).toString(),'fixture');
  const clientDataJSON=Buffer.from(JSON.stringify({type:'webauthn.get',challenge:Buffer.from(publicKey.challenge).toString('base64url'),origin:location.origin}));
  const authenticatorData=Buffer.concat([crypto.createHash('sha256').update(location.hostname).digest(),Buffer.from([5,0,0,0,1])]);
  return {response:{clientDataJSON,authenticatorData,signature:crypto.sign('sha256',Buffer.concat([authenticatorData,crypto.createHash('sha256').update(clientDataJSON).digest()]),pair.privateKey)}};
 }
}}});
(async()=>{
 assert(p.passkeySupported());
 // Independent SDK encoding proves the compact on-chain intent labels retain
 // the exact BCS String bytes used by clients and pre-upgrade queued actions.
 const source=fs.readFileSync('contracts/sources/customer.move','utf8');
 const labels=[...source.matchAll(/b"\\x([0-9a-f]{2})([a-z_]+)"/g)];
 assert.equal(labels.length,53);
 for(const [,length,label] of labels) assert.deepEqual(Buffer.concat([Buffer.from(length,'hex'),Buffer.from(label)]),Buffer.from(bcs.string().serialize(label).toBytes()));
 const key=await p.createPasskey();assert.equal(p.savedPasskeys().length,1);
 const signer=p.passkeySigner(key),bytes=new Uint8Array([1,2,3]);
 const signed=await signer.signTransaction(bytes);
 assert(await new PasskeyPublicKey(Buffer.from(key.publicKey,'base64')).verifyTransaction(bytes,signed.signature));
 assert.equal(await new PasskeyPublicKey(Buffer.from(key.publicKey,'base64')).verifyTransaction(new Uint8Array([1,2,4]),signed.signature),false);
 const backup=p.exportPasskeys();p.importPasskeys('[]');assert.equal(p.savedPasskeys().length,0);p.importPasskeys(backup);p.preferPasskey(key);
 assert.throws(()=>p.importPasskeys('[{}]'),/Invalid/);assert.equal(p.savedPasskeys().length,1);
 const client=manifestClient(m);
 client.getDynamicField=async()=>({dynamicField:{value:{type:`${m.call_package_id}::security_policy::FactorConfiguration`,bcs:wire.security_policy_FactorConfigurationBcs.serialize({binding:Array.from(bcs.vector(bcs.Address).serialize([A,p.passkeyAddress(key)]).toBytes())}).toBytes()}}});
 assert.equal((await p.enrolledSigner(client,C,A)).toSuiAddress(),p.passkeyAddress(key));
 await assert.rejects(p.enrolledSigner(client,C,C),/No enrolled/);
 client.getDynamicField=async()=>{throw new ObjectError('not_found','missing',{reason:'notFound'});};assert.deepEqual(await p.fetchPasskeyEntries(client,C),[]);
 client.getDynamicField=async()=>{throw Error('transport failed');};await assert.rejects(p.fetchPasskeyEntries(client,C),/transport/);
 assert.equal(p.enrollmentProofTx(C,A,key,1000n).getData().commands[0].MoveCall.function,'prove_enrollment');
 assert.equal(p.enrollPasskeyTx(C,C).getData().commands[0].MoveCall.function,'enroll');
 assert.equal(p.removePasskeyTx(C,A,p.passkeyAddress(key)).getData().commands[0].MoveCall.function,'remove');
 global.isSecureContext=false;assert.throws(()=>p.passkeySigner(key),/HTTPS/);
 console.log('PASS passkey native signing, tamper refusal, enrollment builders, on-chain binding, metadata recovery, browser and transport negatives');
})().catch(e=>{console.error(e);process.exitCode=1;});
