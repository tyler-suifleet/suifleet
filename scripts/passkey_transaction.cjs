// Native passkey transaction fixture. Private PEM lives only in --keys (0600).
// stdout contains public metadata or signed transaction bytes, never private keys.
const fs = require('node:fs'), crypto = require('node:crypto');
const { createRequire } = require('node:module');
const req = createRequire(require('node:path').resolve('dapp/package.json'));
const { PasskeyKeypair } = req('@mysten/sui/keypairs/passkey');
const { SuiGrpcClient } = req('@mysten/sui/grpc');
const { Transaction } = req('@mysten/sui/transactions');
// Resolve payment explicitly: the gRPC build plugin simulates full transactions,
// which would prevent expected refusals from reaching validators.
async function resolvePayment(resolved, client, owner, sender, budget) {
 const [{balance}, price] = await Promise.all([
  client.core.getBalance({owner}), client.core.getReferenceGasPrice(),
 ]);
 resolved.setSender(sender);resolved.setGasOwner(owner);resolved.setGasBudget(budget);
 resolved.setGasPrice(price.referenceGasPrice);
 if (BigInt(balance.addressBalance ?? '0') >= BigInt(budget)) {
  const [{systemState}, {chainIdentifier}] = await Promise.all([
   client.core.getCurrentSystemState(), client.core.getChainIdentifier(),
  ]);
  // Coinless payment requires chain-bound replay protection, including a nonce.
  const epoch = BigInt(systemState.epoch);
  resolved.setGasPayment([]);
  resolved.setExpiration({ValidDuring:{minEpoch:String(epoch),maxEpoch:String(epoch+1n),
   minTimestamp:null,maxTimestamp:null,chain:chainIdentifier,nonce:crypto.randomBytes(4).readUInt32LE()}});
 } else {
  let cursor, total = 0n;
  const payments = [], seen = new Set();
  do {
   const page = await client.core.listCoins({owner,cursor});
   for (const coin of page.objects) {
    if (seen.has(coin.objectId)) throw Error('invalid fixture gas pagination');
    seen.add(coin.objectId);
    payments.push({objectId:coin.objectId,version:coin.version,digest:coin.digest});
    total += BigInt(coin.balance);
    if (total >= BigInt(budget) || payments.length === 256) break;
   }
   if (total >= BigInt(budget) || payments.length === 256) break;
   if (page.hasNextPage && (!page.cursor || page.cursor === cursor)) throw Error('invalid fixture gas pagination');
   cursor = page.hasNextPage ? page.cursor : null;
  } while (cursor);
  if (total < BigInt(budget)) throw Error('fixture gas coin unavailable');
  resolved.setGasPayment(payments);
 }
 return resolved.build();
}
module.exports = { resolvePayment };
if (require.main === module) (async () => {
 const input = JSON.parse(fs.readFileSync(0,'utf8'));
 let keys;
 try {
  const stored = fs.readFileSync(input.keys,'utf8');
  try { keys = JSON.parse(stored); } catch { throw Error('Invalid private fixture state'); }
 } catch (error) {
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
 const bytes = await resolvePayment(resolved, client, owner, input.sender, input.budget);
 const signature = input.operation==='proof' ? (await signer(input.key).signTransaction(bytes)).signature : input.sponsor!==undefined ? (await signer(input.sponsor).signTransaction(bytes)).signature : null;
 console.log(JSON.stringify({bytes:Buffer.from(bytes).toString('base64'),signature,digest:await resolved.getDigest()}));
})().catch(e=>{console.error(e.message);process.exitCode=1;});
