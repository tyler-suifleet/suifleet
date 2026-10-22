// Actual Testnet chain/Walrus/Seal reads through the same browser modules.
// The bridge requests only an owner personal-message signature, never a key.
const fs=require('node:fs'),readline=require('node:readline');
const input=JSON.parse(fs.readFileSync(process.argv[2]));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST=JSON.stringify(input.manifest);
process.env.NEXT_PUBLIC_FLEET_DATA=JSON.stringify(input.config);
const {loader,appRequire}=require('./frontend_test_utils.cjs'),load=loader();
const {SuiGrpcClient}=appRequire('@mysten/sui/grpc'),{SessionKey}=appRequire('@mysten/seal');
const {walrus,TESTNET_WALRUS_PACKAGE_CONFIG}=appRequire('@mysten/walrus');
const {toBase64,normalizeSuiAddress:id}=appRequire('@mysten/sui/utils');
const {fetchChainHealth,listFleetDeviceIds,readDeviceData,readFleetData,DATA_CONFIG}=load('lib/fleet-data.ts');
const {readAnchors,fetchVerifiedArchive,archiveDecryptor}=load('lib/fleet-bulk.ts');
const {isValidPersonalMessageSignature}=appRequire('@mysten/sui/verify');
const lines=readline.createInterface({input:process.stdin})[Symbol.asyncIterator]();
(async()=>{
 const client=new SuiGrpcClient({network:'testnet',baseUrl:input.manifest.rpc_url});
 const fleet=await readFleetData(client,DATA_CONFIG);const writer=await readDeviceData(client,DATA_CONFIG,input.device);
 if(!writer || BigInt(writer.anchor_count)!==2n)throw Error('Native archive index did not have exactly two records');
 const page=await listFleetDeviceIds(client,DATA_CONFIG);if(!page.devices.includes(id(input.device)))throw Error('Device missing from fleet index');
 const health=(await fetchChainHealth(client,DATA_CONFIG,[input.device]))[input.device];
 if(!['healthy','degraded'].includes(health.status))throw Error('Health did not verify');
 const budget=(await client.getObject({objectId:input.budgetCap,include:{content:true}})).object;
 if(budget.owner.$kind!=='AddressOwner')throw Error('Budget owner capability is not owned');
 const owner=budget.owner.AddressOwner;
 const session=await SessionKey.create({address:owner,packageId:DATA_CONFIG.typeOrigin,ttlMin:5,suiClient:client});
 console.log(JSON.stringify({personal_message:toBase64(session.getPersonalMessage())}));
 const {signature}=JSON.parse((await lines.next()).value);
 if(!await isValidPersonalMessageSignature(session.getPersonalMessage(),signature,{address:owner}))throw Error('Owner session signature failed independent verification');
 await session.setPersonalMessageSignature(signature);
 const settings={keyServers:input.servers.map(objectId=>({objectId:id(objectId),weight:1})),threshold:2};
 const decrypt=await archiveDecryptor(client,DATA_CONFIG,settings,session,{kind:'owner',ownerCapId:input.budgetCap});
 const storage=client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
 const results=[];const anchors=await readAnchors(client,DATA_CONFIG,input.device,0,2);
 for(let index=0;index<2;index++){
  const archive=await fetchVerifiedArchive(client,storage,DATA_CONFIG,input.device,index,input.manifest.walrus.aggregator_url,decrypt);
  if(archive.reason!==['logs','diagnostics'][index])throw Error('Archive reason/sequence mismatch');
  if(JSON.stringify(archive).includes('sf082-private-fixture-value'))throw Error('Confidential value was not redacted before publication');
  results.push({seq:archive.seq,reason:archive.reason,records:archive.records.length,blobObjectId:anchors[index].blob_object_id,blobId:anchors[index].blob_id,endEpoch:anchors[index].end_epoch});
 }
 console.log(JSON.stringify({result:{verified:true,chain:DATA_CONFIG.chainIdentifier,health:health.status,healthSeq:writer.health_seq,archives:results,ownerSession:true,threshold:2,indexed:true,keyEpoch:fleet.key_epoch}}));
 process.exit(0);
})().catch(()=>{console.error('Live chain/archive/session verification failed; no plaintext logged');process.exit(1)});
