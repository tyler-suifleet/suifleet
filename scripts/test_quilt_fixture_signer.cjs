// Exercise the acceptance-only native bridge with a disposable local key.
// Never prints private material or invokes a network transaction.
const {appRequire} = require('./frontend_test_utils.cjs');
const assert = require('node:assert/strict'), fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const {spawnSync} = require('node:child_process');
const {Ed25519Keypair} = appRequire('@mysten/sui/keypairs/ed25519');
const {decodeSuiPrivateKey} = appRequire('@mysten/sui/cryptography');
const {isValidPersonalMessageSignature} = appRequire('@mysten/sui/verify');
const dir = fs.mkdtempSync(path.join(os.tmpdir(),'quilt-signer-'));
async function main() {
  const key = Ed25519Keypair.generate(), wallet = path.join(dir,'client.yaml'), keystore = path.join(dir,'keystore');
  fs.writeFileSync(wallet,'fixture',{mode:0o600});
  const {secretKey} = decodeSuiPrivateKey(key.getSecretKey());
  fs.writeFileSync(keystore,JSON.stringify([Buffer.concat([Buffer.from([0]),Buffer.from(secretKey)]).toString('base64')]),{mode:0o600});
  secretKey.fill(0);
  const request = {wallet,keystore,expected_address:key.toSuiAddress(),namespace:'0xf0',fleet:'0xf1',device:'0xd0'};
  const base = {schema:1,kind:'fleet-data',chain_identifier:'4c78adac',fleet_id:'0xf1',device_id:'0xd0',signer:key.toSuiAddress(),records:[{source:'daemon',line:'public synthetic Quilt item 0'}]};
  const run = value => spawnSync(process.argv[2],[],{input:JSON.stringify({...request,message:Buffer.from(JSON.stringify(value)).toString('base64')}),encoding:'utf8'});
  const signed = run(base); assert.equal(signed.status,0);
  assert(await isValidPersonalMessageSignature(Buffer.from(JSON.stringify(base)),JSON.parse(signed.stdout).signature,{address:key.toSuiAddress()}));
  for (const change of [{chain_identifier:'35834a8a'},{fleet_id:'0xff'},{device_id:'0xff'},{signer:'0xff'},{schema:99},
    {kind:'transaction'},{records:[{line:'non-fixture private message'}]}]) assert.notEqual(run({...base,...change}).status,0);
  console.log('PASS bounded testnet fixture signer, real personal signature, foreign chain/fleet/device/signer/schema/content refusals; no network');
}
main().catch(error=>{console.error(error.message);process.exitCode=1}).finally(()=>fs.rmSync(dir,{recursive:true,force:true}));
