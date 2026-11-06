const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),cp=require('node:child_process'),net=require('node:net');
const {appRequire}=require('./frontend_test_utils.cjs');
const {Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519');
const {Transaction}=appRequire('@mysten/sui/transactions');
const {verifyTransactionSignature}=appRequire('@mysten/sui/verify');
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'evaluation-signer-test-'));
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 const key=Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(7)),sender=key.toSuiAddress();
 fs.writeFileSync(path.join(directory,'key'),Buffer.alloc(32,7).toString('hex'),{mode:0o600});
 fs.mkdirSync(path.join(directory,'journal'));fs.mkdirSync(path.join(directory,'bin'));
 fs.writeFileSync(path.join(directory,'bin/ticket'),'#!/bin/sh\nexit 0\n',{mode:0o700});
 const pkg='0x'+'a'.repeat(64),device='0x'+'b'.repeat(64),socket=path.join(directory,'sign.sock');
 fs.writeFileSync(path.join(directory,'config.json'),JSON.stringify({key:path.join(directory,'key'),socket,journal:path.join(directory,'journal'),package:pkg,device}));
 const server=cp.spawn(process.execPath,[path.join(__dirname,'evaluation_report_signer.cjs'),path.join(directory,'config.json')],{env:{...process.env,PATH:path.join(directory,'bin')+':'+process.env.PATH,SUIFLEET_TESTNET_LOCK_TOKEN:'offline-fixture'},stdio:'ignore'});
 try{
  for(let i=0;i<100&&!fs.existsSync(socket);i++)await sleep(20);
  const request=body=>new Promise((resolve,reject)=>{let response='';const stream=net.createConnection(socket,()=>stream.write(JSON.stringify(body)+'\n'));stream.on('data',c=>response+=c);stream.on('error',reject);stream.on('end',()=>resolve(JSON.parse(response)));});
  assert.deepEqual(await request({op:'address'}),{address:sender});
  assert((await request({op:'sign_telemetry',message:Buffer.from(JSON.stringify({device_id:device,signer:sender,chain_identifier:'mainnet'})).toString('base64')})).error);
  const tx=new Transaction();tx.setSender(sender);tx.setGasOwner(sender);tx.setGasBudget(10000000);tx.setGasPrice(1000);tx.setGasPayment([{objectId:'0x'+'c'.repeat(64),version:'1',digest:'11111111111111111111111111111111'}]);
  tx.moveCall({target:pkg+'::fleet_data::report_health',arguments:[]});
  const bytes=await tx.build(),digest=await tx.getDigest();
  const result=await request({op:'sign',tx:Buffer.from(bytes).toString('base64')});assert(result.signature);
  assert.equal((await verifyTransactionSignature(bytes,result.signature)).toSuiAddress(),sender);
  const journal=JSON.parse(fs.readFileSync(path.join(directory,'journal',digest+'.json')));assert.equal(journal.digest,digest);assert.equal(journal.tx_bytes,Buffer.from(bytes).toString('base64'));
  assert((await request({op:'sign',tx:Buffer.from(bytes).toString('base64')})).error,'duplicate must stop for reconciliation');
  console.log('PASS: native signer bindings, transaction signature, durable digest/bytes before return and duplicate refusal (offline fixture)');
 }finally{server.kill('SIGTERM');await new Promise(resolve=>server.once('exit',resolve));}
})().finally(()=>fs.rmSync(directory,{recursive:true,force:true})).catch(e=>{console.error(e);process.exitCode=1});
