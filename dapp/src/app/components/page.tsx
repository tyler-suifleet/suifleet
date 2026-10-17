"use client";
import { useState } from 'react';
import { useCurrentAccount,useCurrentClient } from '@mysten/dapp-kit-react';
import { useQuery } from '@tanstack/react-query';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { fetchCatalogs,discoverVersions,buildCreateCatalogTx,buildPublishVersionTx } from '@/lib/catalog';
import { parseVersion } from '@/lib/component-selector';
import { uploadArtifact,uploadRecipe,storageWarnings,renewalCommand } from '@/lib/walrus';
import { PageHeader } from '@/components/ui';
import { ConfigurationFields } from '@/components/ConfigurationFields';
import { EMPTY_CONFIGURATION,recipeConfiguration } from '@/lib/recipe-configuration';
import { assertComponentRecipe,type ComponentRecipe } from '@/lib/recipe-schema';
import Link from 'next/link';
type Row={os:string;arch:string;file?:File;install:string};
export default function ComponentsPage() {
  const account=useCurrentAccount(), client=useCurrentClient();
  const {mutateAsync:execute,isPending}=useFleetTransaction();
  const [catalog,setCatalog]=useState(''),[name,setName]=useState(''),[version,setVersion]=useState('');
  const [rows,setRows]=useState<Row[]>([{os:'linux',arch:'x86_64',install:''}]);
  const [busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const [configuration,setConfiguration]=useState(EMPTY_CONFIGURATION);
  const catalogs=useQuery({queryKey:['catalogs',account?.address],enabled:!!account,queryFn:()=>fetchCatalogs(client,account!.address)});
  const selected=catalog || catalogs.data?.[0]?.objectId || '';
  const versions=useQuery({queryKey:['component-versions',selected],enabled:!!selected,queryFn:()=>discoverVersions(client,selected)});
  function change(i:number,patch:Partial<Row>){setRows(rows.map((r,j)=>j===i?{...r,...patch}:r));}
  async function create(){try{await execute({transaction:buildCreateCatalogTx()});await catalogs.refetch();}catch(e){setMessage(String(e));}}
  async function publish(){
    setBusy(true);setMessage('');
    try {
      if(!/^[A-Za-z0-9_.-]{1,128}$/.test(name)) throw new Error('Invalid component name'); parseVersion(version);
      if(!selected || rows.some(r=>!r.file)) throw new Error('Choose a catalog and an artifact for every platform');
      if(new Set(rows.map(r=>r.os+'/'+r.arch)).size!==rows.length) throw new Error('Duplicate platform');
      // One configuration for the version, in every platform's recipe (SF-015).
      const configured=recipeConfiguration(configuration);
      const recipeFor=(row:Row,artifactBlobId:string,artifactSha256:string):ComponentRecipe=>({schemaVersion:configured.schemaVersion,componentName:name,componentVersion:version,artifactBlobId,artifactSha256,
        lifecycle:{...(row.install?{install:row.install}:{}),...(configured.validate?{validate:configured.validate}:{})},configuration:configured.configuration});
      // Every recipe is checked before the first upload.
      for(const row of rows) assertComponentRecipe(recipeFor(row,'pending','0'.repeat(64)));
      const warnings:string[]=[];
      const manifests=[];
      for(const row of rows){
        const artifact=await uploadArtifact(row.file!,account!.address);
        const recipe=await uploadRecipe(recipeFor(row,artifact.blobId,artifact.sha256),account!.address);
        for(const [label,s] of [['Artifact',artifact.storage],['Recipe',recipe.storage]] as const){
          warnings.push(`${label} storage expires at epoch ${s.endEpoch??'unknown'}`, ...storageWarnings(s));
          const renew=renewalCommand(s);if(renew) warnings.push(renew);
        }
        manifests.push({os:row.os,arch:row.arch,recipeBlobId:recipe.blobId,recipeSha256:recipe.sha256,artifactSha256:artifact.sha256});
      }
      await execute({transaction:buildPublishVersionTx(selected,name,version,manifests)});
      setMessage('Immutable version registered. '+warnings.join('\n'));await versions.refetch();
    }catch(e){setMessage(String(e));}finally{setBusy(false);}
  }
  if(!account)return <p>Connect your wallet to manage component versions.</p>;
  return <div className="space-y-4 max-w-3xl"><PageHeader title="Components" subtitle="Immutable versions with ordered platform manifests. Each device selects the first compatible manifest."/>
    {(catalogs.error||versions.error)&&<p role="alert">{String(catalogs.error||versions.error)}</p>}
    <button onClick={create} disabled={busy||isPending}>Create publisher catalog</button>
    <label className="block">Catalog <select value={selected} onChange={e=>setCatalog(e.target.value)}>{catalogs.data?.map(c=><option key={c.objectId}>{c.objectId}</option>)}</select></label>
    <div className="space-y-3 border p-4"><label className="block">Component name <input value={name} onChange={e=>setName(e.target.value)} /></label>
      <label className="block">Semantic version <input value={version} placeholder="1.0.0" onChange={e=>setVersion(e.target.value)}/></label>
      {rows.map((r,i)=><div key={i} className="border p-3 space-y-2"><p>Manifest {i+1}</p>
        <label>OS <select value={r.os} onChange={e=>change(i,{os:e.target.value})}><option>linux</option><option>macos</option><option>*</option></select></label>{' '}
        <label>Architecture <select value={r.arch} onChange={e=>change(i,{arch:e.target.value})}>{['x86_64','aarch64','arm','x86','*'].map(a=><option key={a}>{a}</option>)}</select></label>
        <label className="block">Artifact <input type="file" onChange={e=>change(i,{file:e.target.files?.[0]})}/></label>
        <label className="block">Optional install command <input value={r.install} onChange={e=>change(i,{install:e.target.value})}/></label>
        {rows.length>1&&<button onClick={()=>setRows(rows.filter((_,j)=>i!==j))}>Remove manifest</button>}
        {i>0&&<button onClick={()=>{const copy=[...rows];[copy[i-1],copy[i]]=[copy[i],copy[i-1]];setRows(copy);}}>Move earlier</button>}
      </div>)}
      <ConfigurationFields value={configuration} onChange={setConfiguration} disabled={busy}/>
      <button disabled={rows.length>=16} onClick={()=>setRows([...rows,{os:'linux',arch:'aarch64',install:''}])}>Add platform</button>{' '}
      <button disabled={busy||isPending||!selected} onClick={publish}>{busy?'Uploading…':'Upload and register immutable version'}</button>
    </div><p className="whitespace-pre-wrap" role="status">{message}</p>
    {versions.data?.map(v=><div key={v.objectId} className="border p-4"><h2>{v.name} {v.version}</h2>{v.manifests.map((m,i)=><p key={i}>{i+1}. {m.os}/{m.arch} · recipe {m.recipeBlobId}</p>)}<Link href={`/firmware?componentVersion=${v.objectId}`}>Deploy this version</Link></div>)}
  </div>;
}
