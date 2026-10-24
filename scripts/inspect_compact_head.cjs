// Read-only verification of real package origins, fleet binding and compact root.
const fs=require('node:fs');
const {loader,appRequire}=require('./frontend_test_utils.cjs');
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc');
const f=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const bulk=loader()('lib/fleet-bulk.ts');
(async()=>{
 const client=new SuiGrpcClient({network:'testnet',baseUrl:f.rpc});
 const head=await bulk.readCompactHead(client,f.config,f.writer);
 if(!head)throw Error('No compact root');
 const anchors=await bulk.readAnchors(client,f.config,f.device,0,64);
 if(!anchors.some(a=>a.seq===head.anchor.seq))throw Error('Compact head is not discoverable');
 console.log(JSON.stringify({head,bcs_hex:Buffer.from(bulk.CompactHeadBcs.serialize(head).toBytes()).toString('hex'),anchors}));
})().catch(e=>{console.error(String(e));process.exit(1)});
