const assert = require('node:assert/strict'), fs=require('node:fs'), crypto=require('node:crypto');
const {loader,appRequire,manifestClient}=require('./frontend_test_utils.cjs');
const catalog=JSON.parse(fs.readFileSync('interfaces/fleet-v1.json')),m=JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
for(const names of [catalog.optional_type_origins.security_policy,catalog.optional_type_origins.customer,catalog.optional_type_origins.passkey_factor,catalog.optional_type_origins.fleet_budget]) for(const n of names)m.type_origins[n]=m.call_package_id;
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

 const {Transaction}=appRequire('@mysten/sui/transactions');
 for (const name of ['approve_action','reject_action','cancel_disclosed_security_action']) {
  const tx=new Transaction();tx.moveCall({target:m.call_package_id+'::customer::'+name,arguments:[]});
  assert.equal(p.approvalDecisionsOnly(tx),true);
  tx.moveCall({target:m.call_package_id+'::customer::set_security',arguments:[]});
  assert.equal(p.approvalDecisionsOnly(tx),false);
 }
 for (const target of ['0xdead::customer::approve_action',m.call_package_id+'::other::approve_action',m.call_package_id+'::customer::propose_approval']) {
  const tx=new Transaction();tx.moveCall({target,arguments:[]});
  assert.equal(p.approvalDecisionsOnly(tx),false);
 }
 assert.equal(p.approvalDecisionsOnly(new Transaction()),false);

 // Real deposit builder + SDK intent resolution: sponsor gas never funds value.
 const budget=load('lib/fleet-budget.ts');
 const sponsor='0x'+'b'.padStart(64,'0');
 for (const tx of [
  budget.buildCreateBudgetTx(123n,budget.DEFAULT_LIMITS,1n,A),
  load('lib/enrollment.ts').buildClaimsTx(C,[],[{claim:A,mist:123n}]),
  load('lib/retention.ts').buildFundRetentionTx({packageId:m.call_package_id,walrus:{walType:'0x2::wal::WAL'}},C,1n,123n),
 ]) p.assertSponsorSafe(p.withSponsorFunding(tx));
 const deposit=p.withSponsorFunding(budget.buildDepositTx(C,A,123n));
 deposit.setSender(A);deposit.setGasOwner(sponsor);
 p.assertSponsorSafe(deposit);
 const owners=[];
 await deposit.toJSON({client:{core:{
  getBalance:async({owner})=>{owners.push(owner);return {balance:{addressBalance:'0',balance:'1000',coinBalance:'1000'}};},
  listCoins:async({owner})=>{owners.push(owner);return {objects:[{objectId:'0x123',version:'1',digest:'11111111111111111111111111111111',balance:'1000'}],hasNextPage:false};},
 }}});
 assert(owners.length>0);assert(owners.every(owner=>owner===A));
 p.assertSponsorSafe(deposit);
 assert(deposit.getData().inputs.some(i=>i.Object?.ImmOrOwnedObject?.objectId.endsWith('0123')));
 // Full SDK build includes gas selection, which intent-only serialization
 // misses. Zero address balance must work with one or several wallet coins.
 const {coreClientResolveTransactionPlugin}=await import(require('node:path').resolve('dapp/node_modules/@mysten/sui/dist/client/core-resolver.mjs'));
 for (const count of [1,3]) for (const sponsored of [false,true]) {
  const queries=[];
  const coins=Array.from({length:count},(_,i)=>({objectId:'0x'+(0x123+i).toString(16).padStart(64,'0'),version:'1',digest:'11111111111111111111111111111111',balance:'10000000000'}));
  const sponsorCoin={...coins[0],objectId:'0x'+'999'.padStart(64,'0')};
  const client={core:{
   resolveTransactionPlugin:()=>coreClientResolveTransactionPlugin,
   getCurrentSystemState:async()=>({systemState:{referenceGasPrice:'1000',epoch:'1'}}),
   getChainIdentifier:async()=>({chainIdentifier:'4c78adac'}),
   getBalance:async({owner})=>{queries.push(['balance',owner]);return {balance:{addressBalance:'0',balance:'30000000000',coinBalance:'30000000000'}};},
   listCoins:async({owner})=>{queries.push(['coins',owner]);return {objects:owner===sponsor?[sponsorCoin]:coins,hasNextPage:false};},
   getObjects:async({objectIds})=>({objects:objectIds.map(objectId=>({objectId,version:'1',digest:coins[0].digest,owner:{$kind:'Shared',Shared:{initialSharedVersion:'1'}},type:'0x2::x::Y'}))}),
   getMoveFunction:async()=>({function:{parameters:[
    {reference:'mutable',body:{$kind:'datatype',datatype:{typeName:'0x2::x::Y',typeParameters:[]}}},
    {reference:'immutable',body:{$kind:'datatype',datatype:{typeName:'0x2::x::Z',typeParameters:[]}}},
    {reference:null,body:{$kind:'datatype',datatype:{typeName:'0x2::coin::Coin',typeParameters:[]}}},
   ]}}),
  }};
  const original=budget.buildDepositTx(C,C,1000000n);
  const tx=sponsored?p.withSponsorFunding(original):original;
  // Conversion must not alter the caller's wallet-paid transaction.
  assert.equal(original.getData().commands[0].$Intent.data.type,'gas');
  tx.setSender(A);if(sponsored)tx.setGasOwner(sponsor);
  tx.setGasBudget(10000000);tx.setGasPrice(1000);
  await tx.build({client});
  const data=tx.getData();
  assert.deepEqual(data.gasData.payment.map(c=>c.objectId),sponsored?[sponsorCoin.objectId]:coins.map(c=>c.objectId));
  if(sponsored){
   p.assertSponsorSafe(tx);
   assert(data.inputs.some(i=>i.Object?.ImmOrOwnedObject?.objectId===coins[0].objectId));
   assert(!data.inputs.some(i=>i.Object?.ImmOrOwnedObject?.objectId===sponsorCoin.objectId));
   assert(queries.some(([kind,owner])=>kind==='coins'&&owner===A));
  } else {
   assert(data.commands.some(c=>c.SplitCoins?.coin.GasCoin));
   assert(!data.inputs.some(i=>i.Object?.ImmOrOwnedObject));
   assert(queries.every(([,owner])=>owner===A));
  }
 }
 const unsafe=new Transaction();
 unsafe.splitCoins(unsafe.gas,[unsafe.pure.u64(123)]);
 assert.throws(()=>p.assertSponsorSafe(unsafe),/wallet-owned funding coins/);
 assert.throws(()=>p.withSponsorFunding(unsafe),/wallet-owned funding coins/);
 const intent=new Transaction();
 intent.add(appRequire('@mysten/sui/transactions').coinWithBalance({balance:123n}));
 assert.throws(()=>p.assertSponsorSafe(intent),/wallet-owned funding coins/);
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
