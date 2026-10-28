const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { appRequire } = require('./frontend_test_utils.cjs');
const { publicKeyFromSuiBytes } = appRequire('@mysten/sui/verify');
const { MultiSigPublicKey } = appRequire('@mysten/sui/multisig');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { verifyTransactionSignature } = appRequire('@mysten/sui/verify');
(async () => {
const descriptor = JSON.parse(fs.readFileSync(path.join(__dirname,'../deployments/testnet/treasury.json'),'utf8'));
assert.equal(descriptor.network,'testnet');
assert.equal(descriptor.chain_identifier,'4c78adac');
assert.equal(descriptor.threshold,2);
assert.equal(descriptor.public_keys.length,3);
assert.equal(new Set(descriptor.public_keys.map(k => k.public_key)).size,3);
const multi = MultiSigPublicKey.fromPublicKeys({threshold:descriptor.threshold,publicKeys:descriptor.public_keys.map(k => {
  assert.equal(k.scheme,'ED25519'); assert.equal(k.weight,1);
  return {publicKey:publicKeyFromSuiBytes(k.public_key),weight:k.weight};
})});
assert.equal(multi.toSuiAddress(),descriptor.treasury_payee);
const dir = fs.mkdtempSync(path.join(os.tmpdir(),'sf185-offline-'));
try {
  const state = path.join(dir,'keys.json');
  const call = mode => execFileSync(process.execPath,[path.join(__dirname,'testnet_treasury.cjs'),mode,state],{encoding:'utf8'});
  const first = JSON.parse(call('create'));
  assert.deepEqual(JSON.parse(call('create')),first,'fixture must reuse its keys');
  const initialFd = fs.openSync(state,fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    assert.equal(fs.fstatSync(initialFd).mode & 0o077,0);
  } finally { fs.closeSync(initialFd); }
  assert.match(call('self-test'),/PASS/);
  const tx = new Transaction();
  tx.setSender(first.treasury_payee); tx.setGasOwner(first.treasury_payee);
  tx.setGasBudget(5_000_000); tx.setGasPrice(1000);
  tx.setGasPayment([{objectId:'0x'+'11'.repeat(32),version:'1',digest:'11111111111111111111111111111111'}]);
  const [coin] = tx.splitCoins(tx.gas,[1]);
  tx.transferObjects([coin],'0x'+'22'.repeat(32));
  const bytes = await tx.build();
  const signature = execFileSync(process.execPath,[path.join(__dirname,'testnet_treasury.cjs'),'sign',state],
    {input:Buffer.from(bytes).toString('base64'),encoding:'utf8'}).trim();
  await verifyTransactionSignature(bytes,signature,{address:first.treasury_payee});
  tx.setSender('0x'+'33'.repeat(32));
  const wrongSender = await tx.build();
  assert.throws(() => execFileSync(process.execPath,[path.join(__dirname,'testnet_treasury.cjs'),'sign',state],
    {input:Buffer.from(wrongSender).toString('base64'),stdio:['pipe','pipe','pipe']}));
  // Unsafe existing files fail closed in both create/reuse and signing modes.
  const utility = path.join(__dirname,'testnet_treasury.cjs');
  const rejected = target => {
    for (const mode of ['create', 'self-test', 'sign']) {
      assert.throws(() => execFileSync(process.execPath,[utility,mode,target],
        {input:'',stdio:['pipe','pipe','pipe'],timeout:5000}));
    }
  };
  const link = path.join(dir,'symlink.json');
  fs.symlinkSync(state,link); rejected(link);
  const hardlink = path.join(dir,'hardlink.json');
  fs.linkSync(state,hardlink); rejected(hardlink); fs.unlinkSync(hardlink);
  fs.chmodSync(state,0o644); rejected(state); fs.chmodSync(state,0o600);
  rejected(dir);
  const fifo = path.join(dir,'fifo');
  execFileSync('mkfifo',[fifo]); rejected(fifo);
  // Deterministically replace the pathname just after descriptor validation.
  // Reads must keep using the validated inode, never the new pathname.
  const preload = path.join(dir,'swap.cjs');
  fs.writeFileSync(preload, `
    const fs = require('node:fs');
    const original = fs.fstatSync;
    let swapped = false;
    fs.fstatSync = function(fd, ...args) {
      const info = original.call(this,fd,...args);
      if (!swapped && info.isFile()) {
        swapped = true;
        fs.renameSync(process.argv[3],process.argv[3]+'.original');
        fs.writeFileSync(process.argv[3],'untrusted replacement',{mode:0o644});
      }
      return info;
    };
  `);
  const swapped = JSON.parse(execFileSync(process.execPath,['--require',preload,utility,'create',state],
    {encoding:'utf8'}));
  assert.deepEqual(swapped,first,'path replacement cannot replace validated keys');
  // Inspect the replacement through its own descriptor too; do not introduce
  // a pathname stat/read pair into the regression test itself.
  const replacementFd = fs.openSync(state,fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    assert.equal(fs.readFileSync(replacementFd,'utf8'),'untrusted replacement');
  } finally { fs.closeSync(replacementFd); }
  console.log('PASS: pinned descriptor; quorum/tamper/wrong-sender rejection; symlink, hardlink, permissions, directory, FIFO and validation/read race regressions');
} finally {fs.rmSync(dir,{recursive:true,force:true});}

})().catch(error => {console.error(error);process.exit(1);});
