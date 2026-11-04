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
 // Inspect and mutate the opened file, so permission checks never race a
 // subsequent path lookup. Keep the descriptor across subprocess checks.
 const fd=fs.openSync(keys,'r+');
 try {
 assert.equal(fs.fstatSync(fd).mode&0o777,0o600);
 const preserved=fs.readFileSync(fd);
 const second=run();assert.equal(second.status,0,second.stderr);assert.equal(second.stdout,first.stdout);
 const reused=Buffer.alloc(preserved.length);fs.readSync(fd,reused,0,reused.length,0);assert.deepEqual(reused,preserved);
 // Corruption must fail closed and must never print the private file content.
 const corrupt='SENSITIVE-CORRUPT-FIXTURE';fs.ftruncateSync(fd,0);fs.writeSync(fd,corrupt,0,'utf8');
 const failed=run();assert.equal(failed.status,1);assert(!failed.stderr.includes(corrupt));const unchanged=Buffer.alloc(Buffer.byteLength(corrupt));fs.readSync(fd,unchanged,0,unchanged.length,0);assert.equal(unchanged.toString('utf8'),corrupt);
 } finally {fs.closeSync(fd);}
 fs.unlinkSync(keys);fs.mkdirSync(keys);
 assert.equal(run().status,1);assert.deepEqual(fs.readdirSync(keys),[]);
 console.log('PASS native fixture creation permissions, identity reuse, corrupt/read-error refusal and secret-safe diagnostics');
} finally {fs.rmSync(root,{recursive:true,force:true});}
