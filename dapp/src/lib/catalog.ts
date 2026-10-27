import type { NamedFleet } from "./customer-fleets";
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction } from '@mysten/sui/transactions';
import { normalizeStructTag } from '@mysten/sui/utils';
import { calls, component_catalog_ComponentVersionBcs as VersionBcs, component_catalog_ComponentCatalogBcs as CatalogBcs, component_catalog_CatalogCreatedBcs as CreatedBcs } from './fleet-protocol';
import { allEvents } from './sui';
import { requireManifest, verifyDeployment, moveType, CATALOG_TYPES, normalizeId } from './deployment-manifest';
import { validateComponent, compareVersions, type PlatformManifest } from './component-selector';
export interface ComponentVersion { objectId:string; catalogId:string; publisher:string; name:string; version:string; manifests:PlatformManifest[] }
export function catalogPackage() {
  const m=requireManifest();
  for(const name of CATALOG_TYPES) if(!m.type_origins[name]) throw new Error('This deployment manifest does not enable component catalogs');
  return m.call_package_id;
}
export function buildCreateCatalogTx() { const tx=new Transaction(); calls.component_catalog_create_catalog(tx,catalogPackage()); return tx; }
export function buildPublishVersionTx(catalog:string,name:string,version:string,manifests:PlatformManifest[]) {
  validateComponent(name,version,manifests);
  const tx=new Transaction();
  calls.component_catalog_publish_version(tx,catalogPackage(),catalog,name,version,manifests.map(m=>m.os),manifests.map(m=>m.arch),manifests.map(m=>m.recipeBlobId),manifests.map(m=>m.recipeSha256),manifests.map(m=>m.artifactSha256),'0x6');
  return tx;
}
export function buildCatalogDeploymentTx(version:string,groups:string[],devices:string[], initiallyPaused = false, fleet?: NamedFleet) {
  const tx=new Transaction();
  if (fleet) {
    const [record] = tx.moveCall({ target: `${catalogPackage()}::customer::new_catalog_deployment`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(version), tx.pure.vector("id", groups), tx.pure.vector("id", devices), tx.object("0x6")] });
    if (initiallyPaused) tx.moveCall({ target: `${catalogPackage()}::customer::set_control`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), record, tx.pure.u8(1), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0)] });
    if (requireManifest().type_origins["deployment_manager::ReceiptShardKey"]) tx.moveCall({ target: `${catalogPackage()}::customer::begin_sharded_receipts`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), record] });
    calls.deployment_manager_share_deployment(tx, catalogPackage(), record);
    return tx;
  }
  if (initiallyPaused) calls.component_catalog_create_deployment_paused(tx,catalogPackage(),version,groups,devices,'0x6');
  else calls.component_catalog_create_deployment(tx,catalogPackage(),version,groups,devices,'0x6');
  return tx;
}
export async function fetchVersion(client:SuiGrpcClient,id:string):Promise<ComponentVersion> {
  catalogPackage(); await verifyDeployment(client);
  const {object}=await client.getObject({objectId:id,include:{content:true}});
  if(normalizeStructTag(object.type)!==normalizeStructTag(moveType('component_catalog::ComponentVersion')) || object.owner.$kind!=='Immutable') throw new Error('Invalid immutable catalog version');
  const v=VersionBcs.parse(object.content);
  if(v.schema_version!=='1') throw new Error('Unsupported catalog schema');
  const n=v.platform_os.length;
  if([v.platform_arch,v.recipe_blob_ids,v.recipe_sha256s,v.artifact_sha256s].some(a=>a.length!==n)) throw new Error('Invalid catalog vectors');
  const manifests=v.platform_os.map((os,i)=>({os,arch:v.platform_arch[i],recipeBlobId:v.recipe_blob_ids[i],recipeSha256:v.recipe_sha256s[i],artifactSha256:v.artifact_sha256s[i]}));
  validateComponent(v.component_name,v.version,manifests);
  return {objectId:object.objectId,catalogId:v.catalog_id,publisher:v.publisher,name:v.component_name,version:v.version,manifests};
}
export async function fetchCatalogs(client:SuiGrpcClient,publisher:string) {
  catalogPackage(); await verifyDeployment(client);
  const events=await allEvents(client,moveType('component_catalog::CatalogCreated'),publisher);
  const ids=[...new Set(events.map(e=>CreatedBcs.parse(e.bcs)).filter(e=>normalizeId(e.publisher)===normalizeId(publisher)).map(e=>e.catalog_id))];
  return Promise.all(ids.map(async id=> {
    const {object}=await client.getObject({objectId:id,include:{content:true}});
    if(normalizeStructTag(object.type)!==normalizeStructTag(moveType('component_catalog::ComponentCatalog')) || object.owner.$kind!=='Shared') throw new Error('Invalid shared catalog');
    const v=CatalogBcs.parse(object.content);
    if(normalizeId(v.publisher)!==normalizeId(publisher)) throw new Error('Catalog publisher mismatch');
    return {objectId:id,publisher:v.publisher,versions:v.versions.contents};
  }));
}
export async function discoverVersions(client:SuiGrpcClient,catalogId:string) {
  catalogPackage(); await verifyDeployment(client);
  const {object}=await client.getObject({objectId:catalogId,include:{content:true}});
  if(normalizeStructTag(object.type)!==normalizeStructTag(moveType('component_catalog::ComponentCatalog')) || object.owner.$kind!=='Shared') throw new Error('Invalid catalog');
  const catalog=CatalogBcs.parse(object.content);
  const versions=await Promise.all(catalog.versions.contents.map(async entry=> {
    const v=await fetchVersion(client,entry.value);
    if(normalizeId(v.catalogId)!==normalizeId(catalogId) || normalizeId(v.publisher)!==normalizeId(catalog.publisher) || entry.key!==`${v.name}@${v.version}`) throw new Error('Catalog index mismatch');
    return v;
  }));
  return versions.sort((a,b)=>a.name.localeCompare(b.name)||-compareVersions(a.version,b.version)||a.objectId.localeCompare(b.objectId));
}

/** Resolve a deployment's immutable version, including bindings moved into receipt shards. */
export async function fetchDeploymentVersion(client: SuiGrpcClient, deploymentId: string): Promise<ComponentVersion> {
  await verifyDeployment(client);
  const { readReceiptDirectory } = await import('./receipt-shards');
  const directory = await readReceiptDirectory(client, deploymentId);
  const { component_catalog_CatalogBindingKeyBcs, component_catalog_ShardedCatalogBindingKeyBcs, component_catalog_CatalogBindingBcs } = await import('./fleet-protocol');
  const sharded = !!directory;
  const name = sharded ? 'component_catalog::ShardedCatalogBindingKey' : 'component_catalog::CatalogBindingKey';
  const key = sharded ? component_catalog_ShardedCatalogBindingKeyBcs : component_catalog_CatalogBindingKeyBcs;
  const result = await client.getDynamicField({ parentId: deploymentId, name: { type: moveType(name), bcs: key.serialize({ dummy_field: false }).toBytes() } });
  if (normalizeStructTag(result.dynamicField.value.type) !== normalizeStructTag(moveType('component_catalog::CatalogBinding'))) throw Error('Invalid deployment catalog binding');
  return fetchVersion(client, component_catalog_CatalogBindingBcs.parse(result.dynamicField.value.bcs).version_id);
}
