"use client";
// SF-037: publish an AWS Greengrass recipe as an immutable catalog version.
// Every rejection and reported difference is shown with its guidance; nothing
// is uploaded until the translation succeeds and the operator acknowledges
// the reported differences. Uploads use the chosen delivery: confidential
// (Seal-encrypted before upload, SF-022) by default, never plaintext fallback.
import { useEffect, useRef, useState } from 'react';
import { artifactUris,parseGreengrassRecipe,translateGreengrassRecipe,GUIDANCE,ipcAclToml,type ArtifactBinding,type Translation } from '@/lib/greengrass-recipe';
import { assertComponentRecipe } from '@/lib/recipe-schema';
import { uploadArtifact,uploadRecipe,storageWarnings,type StorageInfo,type Protection } from '@/lib/walrus';
import { DEFAULT_DELIVERY,type Delivery } from '@/lib/delivery';
import type { AccessPolicy } from '@/lib/access-policy';
import { useReleasePeriod } from '@/hooks/useReleasePeriod';
import { ReleaseStorageStatus } from './ReleaseStorageStatus';
import { ReleasePeriod } from './ReleasePeriod';
import { DeliveryFields } from '@/components/DeliveryFields';
import { FleetStorageChoice } from '@/components/FleetStorageChoice';
import { buildPublishVersionTx } from '@/lib/catalog';
import type { Transaction } from '@mysten/sui/transactions';

async function sha256(file:File){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer())),b=>b.toString(16).padStart(2,'0')).join('');}
// CLI migration guidance retains repository references; the customer UI explains
// the same limits and next steps without internal engineering provenance.
const CUSTOMER_GUIDANCE: Record<string,string> = {
  'unsupported-component-type': 'Only aws.greengrass.generic components translate. Repackage Lambda functions or plugins as a generic component with a run command; the Lambda runtime is not supported.',
  'ipc-access-control': 'accessControl authorizes the AWS IPC protocol, which SuiFleet does not implement. Port the component to the native IPC SDK, run it as a separately provisioned Unix user and have the operator add the suggested [ipc.components.<name>] ACL. Recipes never grant IPC privileges.',
  'aws-managed-dependency': 'This AWS-managed component has no translatable native equivalent yet. Review the dependency results below, provide a native replacement and depend on that component instead.',
  'privileged-step': 'RequiresPrivilege other than false has no native equivalent: components run as the device client user. Move privileged work into a platform installer configured by your administrator.',
};
const guidance=(code:string)=>{
  const key=code.startsWith('ComponentDependencies.')?'aws-managed-dependency':code;
  return CUSTOMER_GUIDANCE[key]??GUIDANCE[key];
};

export function GreengrassImport({catalog,owner,execute,onPublished,disabled,policies,protect,onBusyChange}:{catalog:string;owner:string;execute:(a:{transaction:Transaction})=>Promise<unknown>;onPublished:()=>void;disabled:boolean;policies:AccessPolicy[];protect:(delivery:Delivery)=>Protection;onBusyChange?:(busy:boolean)=>void}) {
  const inFlight=useRef(false),active=useRef(true);
  useEffect(()=>{active.current=true;return ()=>{active.current=false;};},[]);
  const [text,setText]=useState(''),[files,setFiles]=useState<Record<string,File>>({});
  const period=useReleasePeriod(Object.values(files).map(f=>f.size));
  const [result,setResult]=useState<Translation|null>(null),[acknowledged,setAcknowledged]=useState(false);
  const [busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const [stored,setStored]=useState<StorageInfo[]>([]);
  const [delivery,setDelivery]=useState(DEFAULT_DELIVERY);
  const fleet=period.prepaid,[fleetPaid,setFleetPaid]=useState(false);
  const parsed=text.trim()?parseGreengrassRecipe(text):null;
  const uris=parsed&&'recipe' in parsed?artifactUris(parsed.recipe):[];
  // Placeholder blob IDs mark which uploads the translated manifests use.
  async function translate(blob:(uri:string,index:number)=>string){
    if(!parsed) throw new Error('Paste a recipe');
    if(!('recipe' in parsed)) return parsed;
    const bindings:Record<string,ArtifactBinding>={};
    for(const [i,uri] of uris.entries()) if(files[uri]) bindings[uri]={artifactBlobId:blob(uri,i),artifactSha256:await sha256(files[uri])};
    return translateGreengrassRecipe(parsed.recipe,bindings);
  }
  async function check(){setMessage('');setAcknowledged(false);try{setResult(await translate((_,i)=>`pending-${i}`));}catch(e){setMessage(String(e));}}
  async function publish(){
    if(inFlight.current||disabled) return;
    inFlight.current=true;onBusyChange?.(true);
    setBusy(true);setMessage('');
    try{
      const preview=await translate((_,i)=>`pending-${i}`);
      if(preview.outcome!=='translate'||!preview.manifests) throw new Error('The recipe does not translate');
      // Reject an unusable native recipe before any artifact upload.
      for(const m of preview.manifests) assertComponentRecipe(m.recipe);
      if(!catalog) throw new Error('Choose a catalog');
      // One release identity for every artifact and recipe; no plaintext fallback.
      const protection=protect(delivery);
      // Fleet-paid storage is never silently replaced by the publisher.
      if(fleetPaid&&!fleet.store) throw new Error(`Fleet storage is unavailable: ${fleet.reason??'unknown reason'}`);
      if(period.error||!period.epochs||period.loading||period.price===undefined) throw Error(period.error??"Wait for the storage period and price");
      const store=fleetPaid?period.fleetStore:period.publisherStore;
      const used=new Set(preview.manifests.map(m=>m.recipe.artifactBlobId));
      const blobs:StorageInfo[]=[];
      const uploaded:Record<string,{blobId:string;sha256:string}>={},warnings:string[]=[];
      for(const [i,uri] of uris.entries()){
        if(!used.has(`pending-${i}`)) continue;
        const artifact=await uploadArtifact(files[uri],owner,protection,undefined,store);
        blobs.push(artifact.storage);setStored([...blobs]);
        uploaded[uri]=artifact;warnings.push(`Artifact ${uri}: storage expires at epoch ${artifact.storage.endEpoch??'unknown'}`,...storageWarnings(artifact.storage));
      }
      // Translate again with the real uploads; the digests were already checked locally.
      const final=await translate(uri=>uploaded[uri]?.blobId??'unused');
      if(final.outcome!=='translate'||!final.manifests||final.manifests.length!==preview.manifests.length) throw new Error('Translation changed after upload');
      for(const m of final.manifests) assertComponentRecipe(m.recipe);
      const manifests=[];
      for(const m of final.manifests){
        const recipe=await uploadRecipe(m.recipe,owner,protection,undefined,store);
        blobs.push(recipe.storage);setStored([...blobs]);
        warnings.push(`Recipe ${m.os}/${m.arch}: storage expires at epoch ${recipe.storage.endEpoch??'unknown'}`,...storageWarnings(recipe.storage));
        manifests.push({os:m.os,arch:m.arch,recipeBlobId:recipe.blobId,recipeSha256:recipe.sha256,artifactSha256:m.recipe.artifactSha256});
      }
      const {componentName,componentVersion}=final.manifests[0].recipe;
      if(!active.current) throw new Error("Publishing view or account changed; uploaded blobs remain, but registration was cancelled");
      await execute({transaction:buildPublishVersionTx(catalog,componentName,componentVersion,manifests)});
      setMessage(`Translated version registered (${protection.kind==='confidential'?'confidential, Seal-encrypted':'public, unencrypted'}). `+warnings.join('\n'));onPublished();
    }catch(e){setMessage(String(e));}finally{inFlight.current=false;setBusy(false);onBusyChange?.(false);}
  }
  const name=parsed&&'recipe' in parsed&&typeof (parsed.recipe as {ComponentName?:unknown})?.ComponentName==='string'?(parsed.recipe as {ComponentName:string}).ComponentName:'component';
  const differences=result?.informational??[];
  return <div className="space-y-3 border p-4"><h2>Import AWS Greengrass recipe</h2>
    <p>Imports the supported parts of a Greengrass V2 software definition. AWS IPC, Lambda and the nucleus environment are not emulated; unsupported fields are rejected with guidance. Lifecycle steps and artifact keys may use any letter case, as in AWS (Install/Run/Shutdown or install/run/shutdown, URI or Uri). One check lists every finding; bind the listed artifacts and check again.</p>
    <label className="block">Recipe (JSON or YAML)<textarea rows={10} className="w-full font-mono" value={text} onChange={e=>{setText(e.target.value);setResult(null);}}/></label>
    {uris.map(uri=><label key={uri} className="block">Artifact for <code>{uri}</code> <input type="file" onChange={e=>{const f=e.target.files?.[0];setFiles(({[uri]:_,...rest})=>f?{...rest,[uri]:f}:rest);setResult(null);}}/></label>)}
    <button disabled={busy||!text.trim()} onClick={check}>Check translation</button>
    {result&&<div role="status" className="space-y-2">
      <p>{result.outcome==='translate'?`Translates to ${result.manifests!.length} native manifest(s)`:'Rejected'}{result.omitted_manifests?.length?`; manifests ${result.omitted_manifests.join(', ')} omitted (platform not translated: Windows, macOS or 32-bit x86)`:''}</p>
      <ul>{result.reasons.map(r=><li key={r}><code>{r}</code>: {guidance(r)}</li>)}</ul>
      {result.manifests?.map((m,i)=><p key={i}>{i+1}. {m.os}/{m.arch} · recipe {m.recipe.schemaVersion} · lifecycle {Object.keys(m.recipe.lifecycle).join(', ')}</p>)}
      {result.managed_dependencies&&<ul>{Object.entries(result.managed_dependencies).map(([d,c])=><li key={d}><code>{d}</code>: {c.outcome}{c.reason?` (${c.reason})`:''}</li>)}</ul>}
      {result.ipc_acl&&<div><p>Equivalent operator IPC ACL for a ported component (recipes never grant it):</p><pre>{ipcAclToml(name,result.ipc_acl)}</pre>{result.ipc_acl.unmapped.length>0&&<p>No native equivalent: {result.ipc_acl.unmapped.join(', ')}</p>}</div>}
      {differences.length>0&&<div><p>Reported differences from AWS behaviour:</p><ul>{differences.map(d=><li key={d}><code>{d}</code>: {guidance(d)}</li>)}</ul>
        <label><input type="checkbox" checked={acknowledged} onChange={e=>setAcknowledged(e.target.checked)}/> I reviewed these differences</label></div>}
    </div>}
    <DeliveryFields value={delivery} onChange={setDelivery} policies={policies} disabled={busy}/>
    <ReleasePeriod period={period} disabled={busy} fleetPaid={fleetPaid}/>
    <FleetStorageChoice checked={fleetPaid} onChange={setFleetPaid} available={!!fleet.store} storage={fleet.storage} reason={fleet.reason} retention={fleet.retention} disabled={busy}/>
    <button disabled={disabled||busy||result?.outcome!=='translate'||(differences.length>0&&!acknowledged)} onClick={publish}>{busy?'Uploading…':'Upload and register translated version'}</button>
    {stored.map((b,i)=><ReleaseStorageStatus key={i} blobId={b.blobId} blobObjectId={b.blobObjectId}/>)}
    {message&&<p className="whitespace-pre-wrap" role="status">{message}</p>}
  </div>;
}
