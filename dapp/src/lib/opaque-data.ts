// SF139 producer/index reader boundary; SF140 supplies the UI.
import { ObjectError } from '@mysten/sui/client';
import { bcs } from '@mysten/sui/bcs';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { normalizeSuiAddress as id, normalizeStructTag, fromBase64, fromHex, toHex } from '@mysten/sui/utils';
import { isValidPersonalMessageSignature } from '@mysten/sui/verify';
import { EncryptedObject, SealClient, type SessionKey } from '@mysten/seal';
import { Transaction } from '@mysten/sui/transactions';
import type { WalrusClient } from '@mysten/walrus';
import { readFleetData, readDeviceData, type FleetDataConfig } from './fleet-data';
import { readAnchors, requireArchiveReadable, fetchArchiveBytes, type Anchor } from './fleet-bulk';
import { readArchiveQuilt } from './archive-quilt';
import { MAGIC, isEnvelope, type SealSettings } from './seal';
export const DataItemBcs=bcs.struct('DataItem', {
  anchor_index:bcs.u64(), sequence:bcs.u64(), stream:bcs.string(), key:bcs.string(), content_type:bcs.string(),
  metadata:bcs.byteVector(),upload:bcs.string(),chunk:bcs.u32(),chunks:bcs.u32(),total_size:bcs.u64(),size:bcs.u64(),
  sha256:bcs.byteVector(),identifier:bcs.string(),ciphertext_size:bcs.u64(),ciphertext_sha256:bcs.byteVector(),
});
export type DataItem=ReturnType<typeof DataItemBcs.parse>;
// Associate assembly with authenticated provenance, never caller-supplied labels.
const verifiedParts=new WeakMap<Uint8Array,{scope:string;index:string;content:Uint8Array}>();
const hash=async(bytes:Uint8Array)=>new Uint8Array(await crypto.subtle.digest('SHA-256',Uint8Array.from(bytes)));
const equal=(a:ArrayLike<number>,b:ArrayLike<number>)=>a.length===b.length && Array.from(a).every((v,i)=>v===b[i]);
export function validateDataItem(value:DataItem) {
  const length=(s:string)=>new TextEncoder().encode(s).length;
  const metadata=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(new Uint8Array(value.metadata)));
  if(!metadata||Array.isArray(metadata)||typeof metadata!=='object'||Object.keys(metadata).length>16||value.metadata.length>6144
    ||Object.entries(metadata).some(([k,v])=>!k||length(k)>64||typeof v!=='string'||length(v)>256)
    ||!value.stream||length(value.stream)>128||!value.key||length(value.key)>256||!value.content_type||length(value.content_type)>128
    ||!/^[a-fA-F0-9]{64}$/.test(value.upload)||value.chunks<1||value.chunks>2731||value.chunk>=value.chunks
    ||BigInt(value.total_size)>BigInt(67108864)||BigInt(value.sequence)>=BigInt(Number.MAX_SAFE_INTEGER)
    ||value.chunks!==Math.ceil(Math.max(1,Number(value.total_size))/24576)
    ||Number(value.size)!==Math.min(24576,Math.max(0,Number(value.total_size)-value.chunk*24576))
    ||BigInt(value.ciphertext_size)<BigInt(1)||BigInt(value.ciphertext_size)>BigInt(131072)
    ||value.sha256.length!==32||value.ciphertext_sha256.length!==32||!/^data-[0-9]{2}$/.test(value.identifier))throw Error('Opaque descriptor bounds');
}
export async function opaqueOrigin(client:SuiGrpcClient,config:FleetDataConfig) {
  const {object}=await client.getObject({objectId:config.packageId,include:{objectBcs:true}});
  const origins=bcs.Object.parse(object.objectBcs).data.Package?.typeOriginTable;
  const key=origins?.filter(e=>e.moduleName==='fleet_data'&&e.datatypeName==='ItemKey');
  const value=origins?.filter(e=>e.moduleName==='fleet_data'&&e.datatypeName==='DataItem');
  if(key?.length!==1||value?.length!==1||id(key[0].package)!==id(value[0].package))throw Error('Opaque index origin mismatch');
  return id(key[0].package);
}
export async function readDataItem(client:SuiGrpcClient,config:FleetDataConfig,device:string,batch:string,item:number):Promise<DataItem> {
  await readFleetData(client,config);
  if(!Number.isSafeInteger(item)||item<0||item>=16)throw Error('Opaque item index bounds');
  const writer=await readDeviceData(client,config,device);if(!writer)throw Error('Device unavailable');
  const origin=await opaqueOrigin(client,config);
  const field=await client.getDynamicField({parentId:writer.id,name:{type:`${origin}::fleet_data::ItemKey`,
    bcs:bcs.struct('ItemKey',{batch:bcs.u64(),item:bcs.u64()}).serialize({batch,item}).toBytes()}});
  if(normalizeStructTag(field.dynamicField.value.type)!==normalizeStructTag(`${origin}::fleet_data::DataItem`))throw Error('Opaque index type mismatch');
  const value=DataItemBcs.parse(field.dynamicField.value.bcs);
  validateDataItem(value);
  if(BigInt(value.anchor_index)>=BigInt(writer.anchor_count)||BigInt(batch)>BigInt(writer.batch_seq)
    ||BigInt(value.size)>BigInt(24576)||BigInt(value.total_size)>BigInt(67108864)||value.chunks<1||value.chunk>=value.chunks
    ||BigInt(value.ciphertext_size)>BigInt(131072)||value.sha256.length!==32||value.ciphertext_sha256.length!==32
    ||!/^data-[0-9]{2}$/.test(value.identifier))throw Error('Opaque index bounds');
  return value;
}
export type ScopedAuthority = {kind:'owner';ownerCapId:string} | {kind:'reader'}
  | {kind:'customer';customerId:string;fleetId:string};
/** Read-only Seal calls: managed credentials never borrow the stored budget cap. */
export function buildScopedApprovalTx(config:FleetDataConfig,identity:Uint8Array,device:string,
  streamHash:Uint8Array,authority:ScopedAuthority):Transaction {
  const tx=new Transaction();
  const args=[tx.pure.vector('u8',identity),tx.object(config.fleetId)];
  if(authority.kind==='owner')args.push(tx.object(config.budgetId),tx.object(authority.ownerCapId));
  if(authority.kind==='customer')args.push(tx.object(config.budgetId),tx.object(authority.customerId),tx.object(authority.fleetId));
  args.push(tx.pure.id(device),tx.pure.vector('u8',streamHash));
  const fn=authority.kind==='customer'?'seal_approve_scoped_customer':authority.kind==='owner'?'seal_approve_scoped_owner':'seal_approve_scoped_reader';
  tx.moveCall({target:`${config.packageId}::fleet_data::${fn}`,arguments:args});
  return tx;
}
export async function scopedDecryptor(client:SuiGrpcClient,config:FleetDataConfig,settings:SealSettings,
  session:SessionKey,device:string,stream:string,authority:ScopedAuthority) {
  if(session.isExpired()||id(session.getPackageId())!==config.typeOrigin)throw Error('Expired or foreign scoped session');
  const streamHash=await hash(new TextEncoder().encode(stream));
  const seal=new SealClient({suiClient:client,serverConfigs:settings.keyServers,verifyKeyServers:true});
  return async(encrypted:Uint8Array)=>{
    if(session.isExpired())throw Error('Scoped session expired');
    const object=EncryptedObject.parse(encrypted),identity=fromHex(object.id);
    const expected=settings.keyServers.flatMap(s=>Array(s.weight).fill(id(s.objectId))).sort();
    if(id(object.packageId)!==config.typeOrigin||identity.length!==120||!equal(identity.slice(0,32),bcs.Address.serialize(config.fleetId).toBytes())
      ||!equal(identity.slice(40,72),bcs.Address.serialize(device).toBytes())||!equal(identity.slice(72,104),streamHash)
      ||object.threshold!==settings.threshold||object.services.map(([s])=>id(s)).sort().join()!==expected.join())throw Error('Scoped Seal binding mismatch');
    const tx=buildScopedApprovalTx(config,identity,device,streamHash,authority);
    tx.setSender(session.getAddress());
    return seal.decrypt({data:encrypted,sessionKey:session,txBytes:await tx.build({client,onlyTransactionKind:true}),checkShareConsistency:true});
  };
}
export async function openDataItem(ciphertext:Uint8Array,config:FleetDataConfig,device:string,anchor:Anchor,index:DataItem,
  decrypt:(encrypted:Uint8Array)=>Promise<Uint8Array>):Promise<Uint8Array> {
  validateDataItem(index);
  if(ciphertext.length!==Number(index.ciphertext_size)||!isEnvelope(ciphertext)||!equal(await hash(ciphertext),index.ciphertext_sha256))throw Error('Opaque ciphertext digest mismatch');
  const encrypted=ciphertext.slice(MAGIC.length),object=EncryptedObject.parse(encrypted),identity=fromHex(object.id);
  const streamHash=await hash(new TextEncoder().encode(index.stream));
  const aad=new TextEncoder().encode(`suifleet/opaque/v1/${config.chainIdentifier}/${config.fleetId}/${id(device)}/${anchor.seq}/${index.sequence}`);
  if(id(object.packageId)!==config.typeOrigin||identity.length!==120||!equal(identity.slice(0,32),bcs.Address.serialize(config.fleetId).toBytes())
    ||!equal(identity.slice(32,40),bcs.u64().serialize(anchor.key_epoch).toBytes())||!equal(identity.slice(40,72),bcs.Address.serialize(device).toBytes())
    ||!equal(identity.slice(72,104),streamHash)||!object.ciphertext.Aes256Gcm?.aad||!equal(object.ciphertext.Aes256Gcm.aad,aad))throw Error('Opaque Seal scope mismatch');
  const plaintext=await decrypt(encrypted);
  let message:Uint8Array|undefined;
  try {
    if(plaintext.length>131072)throw Error('Opaque plaintext limit');
    const envelope=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(plaintext));
    message=fromBase64(envelope.message);
    if(!await isValidPersonalMessageSignature(message,envelope.signature,{address:anchor.signer}))throw Error('Opaque signature mismatch');
    const value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(message)),item=value.item,m=item?.descriptor;
    if(value.kind!=='fleet-opaque'||value.schema!==1||value.chain_identifier!==config.chainIdentifier||id(value.registry)!==config.registryId
      ||id(value.fleet_id)!==config.fleetId||id(value.device_id)!==id(device)||id(value.signer)!==id(anchor.signer)
      ||String(value.batch_sequence)!==anchor.seq||String(item.sequence)!==index.sequence)throw Error('Opaque signed binding mismatch');
    for(const key of ['stream','key','content_type','upload','chunk','chunks','total_size','size'] as const)if(String(m[key])!==String(index[key]))throw Error('Opaque metadata mismatch');
    if(!equal(m.sha256,index.sha256)||JSON.stringify(Object.entries(m.metadata).sort(([a],[b])=>a<b?-1:a>b?1:0))!==JSON.stringify(Object.entries(JSON.parse(new TextDecoder().decode(new Uint8Array(index.metadata)))).sort(([a],[b])=>a<b?-1:a>b?1:0)))throw Error('Opaque signed metadata mismatch');
    const bytes=fromBase64(item.data);
    if(bytes.length!==Number(index.size)||!equal(await hash(bytes),index.sha256))throw Error('Opaque content digest mismatch');
    verifiedParts.set(bytes,{scope:`${config.chainIdentifier}/${config.registryId}/${config.fleetId}/${id(device)}`,
      index:toHex(DataItemBcs.serialize(index).toBytes()),content:Uint8Array.from(bytes)});
    return bytes;
  } finally {message?.fill(0);plaintext.fill(0);}
}
export async function fetchDataItem(client:SuiGrpcClient,walrus:Pick<WalrusClient,'systemState'>,config:FleetDataConfig,
  device:string,batch:string,item:number,aggregator:string,decrypt:(encrypted:Uint8Array)=>Promise<Uint8Array>) {
  const index=await readDataItem(client,config,device,batch,item);
  const [anchor]=await readAnchors(client,config,device,Number(index.anchor_index),1);
  if(!anchor||anchor.seq!==batch)throw Error('Opaque anchor mismatch');
  let state=await walrus.systemState();await requireArchiveReadable(client,config,device,anchor,state.committee.epoch);
  const url=new URL(aggregator);
  if(url.username||url.password||url.search||url.hash||(url.protocol!=='https:'&&!(url.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(url.hostname))))throw Error('Invalid aggregator');
  const {blobIdFromInt}=await import('@mysten/walrus');
  const parent=await fetchArchiveBytes(`${aggregator.replace(/\/+$/,'')}/v1/blobs/${blobIdFromInt(anchor.blob_id)}`);
  if(state.committee.n_shards!==1000)throw Error('Unsupported Quilt profile');
  const bytes=readArchiveQuilt(parent).get(index.identifier);if(!bytes)throw Error('Opaque patch unavailable');
  const result=await openDataItem(bytes,config,device,anchor,index,decrypt);
  try {
    state=await walrus.systemState();await requireArchiveReadable(client,config,device,anchor,state.committee.epoch);
    const refreshed=await readDataItem(client,config,device,batch,item);
    if(toHex(DataItemBcs.serialize(refreshed).toBytes())!==toHex(DataItemBcs.serialize(index).toBytes()))throw Error('Opaque index changed');
    return result;
  } catch(error) {result.fill(0);throw error;}
}
export async function setReadGrant(tx:Transaction,config:FleetDataConfig,ownerCapId:string,device:string,stream:string|undefined,reader:string,allowed:boolean) {
  tx.moveCall({target:`${config.packageId}::fleet_data::set_read_grant`,arguments:[tx.object(config.fleetId),tx.object(config.budgetId),tx.object(ownerCapId),
    tx.pure.id(device),tx.pure.vector('u8',stream===undefined?[]:await hash(new TextEncoder().encode(stream))),tx.pure.address(reader),tx.pure.bool(allowed)]});
}
/** Reassemble only a complete verified upload; missing/duplicate chunks fail. */
export function assembleDataItems(parts:{index:DataItem;bytes:Uint8Array}[]) {
  if(!parts.length)throw Error('Empty opaque upload');
  const sorted=[...parts].sort((a,b)=>a.index.chunk-b.index.chunk),first=sorted[0].index;
  if(parts.length!==first.chunks||BigInt(first.total_size)>BigInt(67108864))throw Error('Incomplete opaque upload');
  const provenance=verifiedParts.get(sorted[0].bytes);if(!provenance)throw Error('Unverified opaque chunk');
  const result=new Uint8Array(Number(first.total_size));let offset=0;
  for(let n=0;n<sorted.length;n++) {
    const {index,bytes}=sorted[n],verified=verifiedParts.get(bytes);
    if(!verified||verified.scope!==provenance.scope||verified.index!==toHex(DataItemBcs.serialize(index).toBytes())||!equal(bytes,verified.content))throw Error('Opaque chunk provenance mismatch');
    if(index.chunk!==n||index.chunks!==first.chunks||index.upload!==first.upload||index.stream!==first.stream
      ||index.key!==first.key||index.content_type!==first.content_type||index.total_size!==first.total_size
      ||bytes.length!==Number(index.size)||offset+bytes.length>result.length)throw Error('Opaque chunk binding mismatch');
    result.set(bytes,offset);offset+=bytes.length;
  }
  if(offset!==result.length)throw Error('Opaque upload length mismatch');
  return result;
}
/** Bounded public metadata page; cursor traverses all writer fields, including legacy anchors. */
export async function listDataItems(client:SuiGrpcClient,config:FleetDataConfig,device:string,cursor?:string|null) {
  await readFleetData(client,config);
  const writer=await readDeviceData(client,config,device);if(!writer)throw Error('Device unavailable');
  const origin=await opaqueOrigin(client,config);
  const page=await client.listDynamicFields({parentId:writer.id,cursor,limit:32});
  if(page.dynamicFields.length>32||(page.hasNextPage&&(!page.cursor||page.cursor===cursor)))throw Error('Invalid opaque index page');
  const items:{batch:string;item:number;value:DataItem}[]=[];
  const key=bcs.struct('ItemKey',{batch:bcs.u64(),item:bcs.u64()});
  for(const field of page.dynamicFields) {
    if(normalizeStructTag(field.name.type)!==normalizeStructTag(`${origin}::fleet_data::ItemKey`))continue;
    if(field.$kind!=='DynamicField'||normalizeStructTag(field.valueType)!==normalizeStructTag(`${origin}::fleet_data::DataItem`))throw Error('Invalid opaque index field');
    const name=key.parse(field.name.bcs);if(BigInt(name.item)>=BigInt(16))throw Error('Invalid opaque item ordinal');
    items.push({batch:name.batch,item:Number(name.item),value:await readDataItem(client,config,device,name.batch,Number(name.item))});
  }
  return {items,cursor:page.cursor,hasNextPage:page.hasNextPage};
}

/** Discovery is a locator hint only. Seal checks the exact device/stream grant
 * again on every decrypt; listing a fleet never grants customer authority. */
export async function hasScopedReadGrant(client:SuiGrpcClient,config:FleetDataConfig,reader:string) {
  await readFleetData(client,config);
  const {object}=await client.getObject({objectId:config.packageId,include:{objectBcs:true}});
  const origins=bcs.Object.parse(object.objectBcs).data.Package?.typeOriginTable;
  const entries=origins?.filter(e=>e.moduleName==='fleet_data'&&e.datatypeName==='ReadGrantKey') ?? [];
  if(!entries.length)return false; // Older data packages have no scoped grants.
  if(entries.length!==1)throw Error('Read grant origin mismatch');
  const type=`${id(entries[0].package)}::fleet_data::ReadGrantKey`;
  const key=bcs.struct('ReadGrantKey',{device_id:bcs.Address,stream_hash:bcs.byteVector(),reader:bcs.Address});
  let cursor:string|undefined;
  const seen=new Set<string>();
  for(let pages=0;pages<100;pages++) {
    const page=await client.listDynamicFields({parentId:config.fleetId,cursor,limit:100});
    if(page.dynamicFields.length>100||(page.hasNextPage&&(!page.cursor||seen.has(page.cursor))))throw Error('Invalid read grant page');
    for(const field of page.dynamicFields) {
      if(normalizeStructTag(field.name.type)!==normalizeStructTag(type))continue;
      if(field.$kind!=='DynamicField'||field.valueType!=='bool')throw Error('Invalid read grant field');
      const grant=key.parse(field.name.bcs);
      if(grant.stream_hash.length!==0&&grant.stream_hash.length!==32)throw Error('Invalid read grant scope');
      if(id(grant.reader)!==id(reader))continue;
      // Re-read the exact key: revoked/stale index entries confer no access.
      try {
        await client.getDynamicField({parentId:config.fleetId,name:{type,bcs:field.name.bcs}});
        return true;
      } catch(error) {
        if(error instanceof ObjectError && error.reason==='notFound')continue;
        throw error;
      }
    }
    if(!page.hasNextPage)return false;
    cursor=page.cursor!;seen.add(cursor);
  }
  throw Error('Read grant discovery exceeds browser limits');
}
