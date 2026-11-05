// Disposable acceptance signer: journals exact native transaction identity before
// signing. This is test instrumentation, not the shipped protected signing service.
const fs=require('node:fs'),net=require('node:net'),cp=require('node:child_process');
const {appRequire}=require('./frontend_test_utils.cjs');
const {Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519');
const {Transaction}=appRequire('@mysten/sui/transactions');
const cfg=JSON.parse(fs.readFileSync(process.argv[2]));
const key=Ed25519Keypair.fromSecretKey(Buffer.from(fs.readFileSync(cfg.key,'utf8').trim(),'hex'));
const address=key.toSuiAddress();
const server=net.createServer(socket=>{
 let text='';socket.on('data',async chunk=>{
  text+=chunk.toString();if(text.length>2*1024*1024){socket.destroy();return;}
  if(!text.includes('\n'))return;socket.pause();
  try{
   const request=JSON.parse(text.split('\n')[0]);let result;
   if(request.op==='address')result={address};
   else if(request.op==='sign_telemetry'){
    const message=Buffer.from(request.message,'base64'),summary=JSON.parse(message.toString());
    if(summary.device_id!==cfg.device||summary.chain_identifier!=='4c78adac'||summary.signer!==address)throw Error('Telemetry binding mismatch');
    result={signature:(await key.signPersonalMessage(message)).signature};
   }else if(request.op==='sign'){
    const bytes=Buffer.from(request.tx,'base64'),tx=Transaction.from(bytes),data=tx.getData();
    if(data.sender!==address||data.gasData.owner!==address||BigInt(data.gasData.budget)>50000000n)throw Error('Unexpected sender/gas');
    const call=data.commands[0]?.MoveCall;
    if(data.commands.length!==1||!call||call.package!==cfg.package||call.module!=='fleet_data'||call.function!=='report_health')throw Error('Unexpected native call');
    const lock=cp.spawnSync('ticket',['--project','suifleet','lock-assert','network:suifleet-testnet','--lock-token',process.env.SUIFLEET_TESTNET_LOCK_TOKEN],{stdio:'pipe'});
    if(lock.status!==0)throw Error('Mutation lease invalid');
    if(fs.readdirSync(cfg.journal).length)throw Error('Prior native transaction requires effects reconciliation');
    const digest=await tx.getDigest(),path=cfg.journal+'/'+digest+'.json';
    const fd=fs.openSync(path,'wx',0o600);
    try{fs.writeFileSync(fd,JSON.stringify({digest,sender:address,gas_budget:data.gasData.budget,tx_bytes:request.tx}));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
    const dir=fs.openSync(cfg.journal,'r');try{fs.fsyncSync(dir);}finally{fs.closeSync(dir);}
    result={signature:(await key.signTransaction(bytes)).signature};
   }else throw Error('Unexpected operation');
   socket.end(JSON.stringify(result)+'\n');
  }catch{socket.end(JSON.stringify({error:'Acceptance signer refused; inspect private runtime'})+'\n');}
 });
});
server.listen(cfg.socket);process.on('SIGTERM',()=>server.close(()=>process.exit(0)));
