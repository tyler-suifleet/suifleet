// Real subprocess/private-file regressions for the native passkey fixture.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawnSync}=require('node:child_process');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'sf198-fixture-'));
const keys=path.join(root,'keys.json');
const run=()=>spawnSync(process.execPath,['scripts/passkey_transaction.cjs'],{input:JSON.stringify({operation:'info',keys}),encoding:'utf8'});
try {
 const first=run();assert.equal(first.status,0,first.stderr);
 const metadata=JSON.parse(first.stdout);assert.equal(metadata.length,2);
 assert(metadata.every(k=>k.address.startsWith('0x')&&!('privateKey' in k)));
 assert.equal(fs.statSync(keys).mode&0o777,0o600);
 const preserved=fs.readFileSync(keys);
 const second=run();assert.equal(second.status,0,second.stderr);assert.equal(second.stdout,first.stdout);
 assert.deepEqual(fs.readFileSync(keys),preserved);
 // Corruption must fail closed and must never print the private file content.
 const corrupt='SENSITIVE-CORRUPT-FIXTURE';fs.writeFileSync(keys,corrupt);
 const failed=run();assert.equal(failed.status,1);assert(!failed.stderr.includes(corrupt));assert.equal(fs.readFileSync(keys,'utf8'),corrupt);
 fs.unlinkSync(keys);fs.mkdirSync(keys);
 assert.equal(run().status,1);assert(fs.statSync(keys).isDirectory());
 console.log('PASS native fixture creation permissions, identity reuse, corrupt/read-error refusal and secret-safe diagnostics');
} finally {fs.rmSync(root,{recursive:true,force:true});}
