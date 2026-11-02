// Native passkey transaction fixture. Private PEM lives only in --keys (0600).
// stdout contains public metadata or signed transaction bytes, never private keys.
const fs = require('node:fs'), crypto = require('node:crypto');
const { createRequire } = require('node:module');
const req = createRequire(require('node:path').resolve('dapp/package.json'));
const { PasskeyKeypair } = req('@mysten/sui/keypairs/passkey');
const { SuiGrpcClient } = req('@mysten/sui/grpc');
const { Transaction } = req('@mysten/sui/transactions');
(async () => {
 const input = JSON.parse(fs.readFileSync(0,'utf8'));
 let keys;
 try {
  const stored = fs.readFileSync(input.keys,'utf8');
  try { keys = JSON.parse(stored); } catch { throw Error('Invalid private fixture state'); }
 }
 catch (error) {
  if (error.code !== 'ENOENT') throw error;
  keys = Array.from({length:2}, () => crypto.generateKeyPairSync('ec',{namedCurve:'prime256v1',privateKeyEncoding:{type:'pkcs8',format:'pem'},publicKeyEncoding:{type:'spki',format:'der'}})).map(k=>({privateKey:k.privateKey, publicKey:crypto.ECDH.convertKey(k.publicKey.subarray(-65),'prime256v1',undefined,undefined,'compressed').toString('base64')}));
  fs.writeFileSync(input.keys,JSON.stringify(keys),{mode:0o600,flag:'wx'});
 }
 function signer(index) {
  const key = keys[index];
  return new PasskeyKeypair(Buffer.from(key.publicKey,'base64'), {
   create: async()=>{throw Error('fixture registration uses explicit native public key');},
   get: async challenge=>{
    const clientDataJSON=Buffer.from(JSON.stringify({type:'webauthn.get',challenge:Buffer.from(challenge).toString('base64url'),origin:'https://fixture.suifleet.invalid',crossOrigin:false}));
    const authenticatorData=Buffer.concat([crypto.createHash('sha256').update('fixture.suifleet.invalid').digest(),Buffer.from([5,0,0,0,1])]);
    const signature=crypto.sign('sha256',Buffer.concat([authenticatorData,crypto.createHash('sha256').update(clientDataJSON).digest()]),key.privateKey);
    return {response:{clientDataJSON,authenticatorData,signature}};
   }
  });
 }
 if(input.operation==='info') { console.log(JSON.stringify(keys.map((k,i)=>({publicKey:k.publicKey,address:signer(i).toSuiAddress()})))); return; }
 const client = new SuiGrpcClient({network:input.network,baseUrl:input.endpoint});
 const tx = new Transaction(); tx.setSender(input.sender);tx.setGasBudget(input.budget);
 if(input.sponsor!==undefined) tx.setGasOwner(signer(input.sponsor).toSuiAddress());
 if(input.operation==='proof') tx.moveCall({target:`${input.package}::passkey_factor::prove_enrollment`,arguments:[tx.object(input.customer),tx.pure.address(input.administrator),tx.pure.vector('u8',Buffer.from(keys[input.key].publicKey,'base64')),tx.pure.u64(input.expires),tx.object('0x6')]});
 else if(input.operation==='rename') tx.moveCall({target:`${input.package}::customer::rename_fleet`,arguments:[tx.object(input.customer),tx.object(input.fleet),tx.pure.string(input.name)]});
 else if(input.operation==='policy') tx.moveCall({target:`${input.package}::customer::set_security`,arguments:[tx.object(input.customer),tx.pure.vector('u8',Array(7).fill(input.mask))]});
 else if(input.operation==='remove') tx.moveCall({target:`${input.package}::passkey_factor::remove`,arguments:[tx.object(input.customer),tx.pure.address(input.administrator),tx.pure.address(signer(input.key).toSuiAddress())]});
 else throw Error('unsupported fixture operation');
 // Resolve inputs without requiring simulated success: refused-action tests
 // must reach validators and record failed effects, rather than stop at preflight.
 const kind = await tx.build({client,onlyTransactionKind:true});
 const resolved = Transaction.fromKind(kind);
 const owner = input.sponsor!==undefined ? signer(input.sponsor).toSuiAddress() : input.sender;
 const coins = await client.core.listCoins({owner});
 const coin = coins.objects.find(c=>BigInt(c.balance)>=BigInt(input.budget));
 if(!coin) throw Error('fixture gas coin unavailable');
 const price = await client.core.getReferenceGasPrice();
 resolved.setSender(input.sender);resolved.setGasOwner(owner);
 resolved.setGasBudget(input.budget);resolved.setGasPrice(price.referenceGasPrice);
 resolved.setGasPayment([{objectId:coin.objectId,version:coin.version,digest:coin.digest}]);
 const bytes = await resolved.build();
 const signature = input.operation==='proof' ? (await signer(input.key).signTransaction(bytes)).signature : input.sponsor!==undefined ? (await signer(input.sponsor).signTransaction(bytes)).signature : null;
 console.log(JSON.stringify({bytes:Buffer.from(bytes).toString('base64'),signature,digest:await resolved.getDigest()}));
})().catch(e=>{console.error(e.message);process.exitCode=1;});
