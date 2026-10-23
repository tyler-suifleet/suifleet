const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {pathToFileURL}=require('node:url');const {appRequire}=require('./frontend_test_utils.cjs');
const {EncryptedObject}=appRequire('@mysten/seal');
(async()=>{
 const root=path.dirname(appRequire.resolve('@mysten/seal'));
 const {decrypt}=await import(pathToFileURL(path.join(root,'decrypt.mjs')));
 const {G1Element,G2Element}=await import(pathToFileURL(path.join(root,'bls12381.mjs')));
 const v=JSON.parse(fs.readFileSync('interfaces/seal-vectors.json'));
 const cipher=EncryptedObject.parse(Buffer.from(fs.readFileSync(process.argv[2],'utf8').trim(),'hex'));
 const fullId=v.full_id.replace(/^0x/,''),keys=new Map(v.servers.map(s=>[`${fullId}:${s.object_id}`,G1Element.fromBytes(Buffer.from(s.user_secret_key,'hex'))]));
 const publicKeys=v.servers.map(s=>G2Element.fromBytes(Buffer.from(s.public_key,'hex')));
 assert.equal(Buffer.from(await decrypt({encryptedObject:cipher,keys,publicKeys,checkLEEncoding:false})).toString(),'public native encryption test fixture');
 keys.delete(keys.keys().next().value);
 assert.equal(Buffer.from(await decrypt({encryptedObject:cipher,keys,publicKeys,checkLEEncoding:false})).toString(),'public native encryption test fixture');
 keys.delete(keys.keys().next().value);
 await assert.rejects(decrypt({encryptedObject:cipher,keys,publicKeys,checkLEEncoding:false}),/Not enough shares/);
 console.log('Native Seal encryption independently decrypted by pinned SDK; consistency and threshold verified');
})().catch(e=>{console.error(e.message);process.exit(1)});
