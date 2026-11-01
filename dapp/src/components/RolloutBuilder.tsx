"use client";
import { useState } from 'react';
import { useCurrentAccount } from '@/hooks/useCurrentAccount';
import { useCurrentClient } from '@mysten/dapp-kit-react';
import { useQuery } from '@tanstack/react-query';
import { useDevices } from '@/hooks/useDevices';
import { fetchFleetDevices } from '@/lib/sui';
import { useGroups } from '@/hooks/useGroups';
import { fetchCatalogs, discoverVersions } from '@/lib/catalog';
import { libraryRecipe, policyInput, type DurationUnit } from '@/lib/rollout-form';
import type { RolloutInput } from '@/lib/rollout';

const defaults: RolloutInput = { component:'', version:'', recipe:'', artifactSha256:'', recipeSha256:'', cohorts:[], concurrency:1, rateCount:1, rateWindowMs:60000, bakeMs:300000, minimumSamples:1, failureBps:1000, offlineTimeoutMs:3600000 };
export function RolloutBuilder({ busy, create }: { busy: boolean; create: (input: RolloutInput) => Promise<void> }) {
  const client = useCurrentClient(), account = useCurrentAccount();
  const devices = useDevices(), groups = useGroups();
  const library = useQuery({ queryKey:['rollout-library', account?.address], enabled:!!account,
    queryFn:async () => (await Promise.all((await fetchCatalogs(client, account!.address)).map(c => discoverVersions(client,c.objectId)))).flat() });
  const [input,setInput] = useState(defaults), [cohorts,setCohorts] = useState<string[][]>([[]]);
  const [source,setSource] = useState(''), [search,setSearch] = useState(''), [active,setActive] = useState(0);
  const [durations,setDurations] = useState<{rate:[number,DurationUnit]; bake:[number,DurationUnit]; stall:[number,DurationUnit]}>({rate:[1,'minutes'],bake:[5,'minutes'],stall:[1,'hours']});
  const [complete,setComplete] = useState(false);
  const [percent,setPercent] = useState(10), [review,setReview] = useState<RolloutInput | null>(null), [error,setError] = useState('');
  const missing = [...new Set(groups.data?.flatMap(g=>g.deviceIds) ?? [])].filter(id=>!devices.data?.some(d=>d.objectId===id));
  const groupDevices = useQuery({queryKey:['rollout-group-devices',account?.address,...missing],enabled:missing.length>0,queryFn:()=>fetchFleetDevices(client,missing)});
  const allDevices = [...(devices.data ?? []),...(groupDevices.data ?? [])];
  const names = new Map(allDevices.map(d => [d.objectId,d.name]));
  function add(ids:string[]) { setCohorts(old => old.map((c,i) => i === active ? [...c,...ids.filter(id => !old.flat().includes(id))] : c)); }
  function move(i:number, delta:number) { const next = cohorts.map(c => [...c]); [next[i],next[i+delta]]=[next[i+delta],next[i]]; setCohorts(next); setActive(i+delta); }
  return <details className="border p-5"><summary>Create a gradual rollout</summary>
    <p className="my-3">Return here to approve each step. Creation admits no devices. Cohorts are immutable snapshots: later group membership changes never change this rollout.</p>
    <form noValidate onSubmit={e => { e.preventDefault(); setError(''); try { setReview(policyInput({...input,cohorts},durations,percent)); } catch(e) { setError(String(e)); } }}>
      <fieldset disabled={busy || !!review || complete} className="space-y-4 disabled:opacity-60">
        <label className="block">Published library version and platform<select className="block border p-2 w-full" value={source} onChange={e => { setSource(e.target.value); const [id,index]=e.target.value.split('/'); const v=library.data?.find(v=>v.objectId===id); setInput(v ? {...input,...libraryRecipe(v,Number(index))} : defaults); }}><option value="">Choose a recipe…</option>{library.data?.flatMap(v=>v.manifests.map((m,i)=><option key={`${v.objectId}/${i}`} value={`${v.objectId}/${i}`}>{v.name} {v.version} · {m.os}/{m.arch}</option>))}</select></label>
        {library.isLoading && <p role="status">Loading library…</p>}{library.error && <p role="alert">Library unavailable: {library.error.message}</p>}
        <p>Each rollout uses one platform recipe. Choose compatible devices. <a href="/components" className="underline">Publish or browse library versions</a>.</p>
        <details><summary>Advanced: direct upload recipe or manual entry</summary><p>Copy the recipe blob ID and both SHA-256 digests from a completed direct upload. Digests must contain exactly 64 lowercase hexadecimal characters.</p>{(['component','version','recipe','artifactSha256','recipeSha256'] as const).map(k=><label className="block" key={k}>{k}<input required className="block border p-2 w-full" value={input[k]} readOnly={!!source} onChange={e=>setInput({...input,[k]:e.target.value})}/></label>)}</details>
        <label className="block">Search named devices and groups<input type="search" className="block border p-2 w-full" value={search} onChange={e=>setSearch(e.target.value)}/></label>
        {(devices.error || groups.error || groupDevices.error) && <p role="alert">Target inventory unavailable: {String(devices.error || groups.error || groupDevices.error)}</p>}
        <div className="flex flex-wrap gap-2">{cohorts.map((c,i)=><div className="border p-2" key={i}><button type="button" aria-pressed={active===i} onClick={()=>setActive(i)}>Cohort {i+1} ({c.length})</button>{i>0 && <button type="button" onClick={()=>move(i,-1)}>Move earlier</button>}{i<cohorts.length-1 && <button type="button" onClick={()=>move(i,1)}>Move later</button>}{cohorts.length>1 && <button type="button" onClick={()=>{setCohorts(cohorts.filter((_,j)=>j!==i));setActive(0);}}>Remove cohort</button>}</div>)}<button type="button" onClick={()=>{setCohorts([...cohorts,[]]);setActive(cohorts.length);}}>Add cohort</button></div>
        <p>Add targets to cohort {active+1}. A device can belong to only one cohort.</p>
        <div className="max-h-60 overflow-auto border p-2">{groups.data?.filter(g=>g.name.toLowerCase().includes(search.toLowerCase())).map(g=><div key={g.objectId}><button type="button" onClick={()=>add(g.deviceIds)}>Add group {g.name} ({g.deviceIds.length})</button></div>)}{allDevices.filter(d=>`${d.name} ${d.objectId}`.toLowerCase().includes(search.toLowerCase())).map(d=><label className="block" key={d.objectId}><input type="checkbox" checked={cohorts[active]?.includes(d.objectId) ?? false} disabled={cohorts.some((c,i)=>i!==active && c.includes(d.objectId))} onChange={e=>e.target.checked ? add([d.objectId]) : setCohorts(cohorts.map((c,i)=>i===active ? c.filter(id=>id!==d.objectId):c))}/>{d.name}<details><summary>Object ID</summary>{d.objectId}</details></label>)}</div>
        {cohorts.map((c,i)=><div key={i}>Cohort {i+1}: {c.map((id,index)=><span key={id} className="inline-block border p-1 m-1">{names.get(id) ?? 'Unavailable device'} <button type="button" aria-label={`Remove ${names.get(id) ?? id} from cohort ${i+1}`} onClick={()=>setCohorts(cohorts.map((v,j)=>j===i?v.filter(x=>x!==id):v))}>×</button>{index>0 && <button type="button" aria-label={`Move ${names.get(id) ?? id} earlier`} onClick={()=>setCohorts(cohorts.map((v,j)=>{if(j!==i)return v; const next=[...v];[next[index-1],next[index]]=[next[index],next[index-1]];return next;}))}>←</button>}<details><summary>ID</summary>{id}</details></span>)}</div>)}
        {(['concurrency','rateCount','minimumSamples'] as const).map((k,i)=><label className="block" key={k}>{['Concurrent outstanding grants','Grants per rolling window','Minimum samples per cohort'][i]}<input required type="number" min="1" max="512" value={input[k]} onChange={e=>setInput({...input,[k]:Number(e.target.value)})} className="border p-2 ml-2"/></label>)}
        {(['rate','bake','stall'] as const).map((k,i)=><label className="block" key={k}>{['Rate window','Bake period','Stall timeout before pause'][i]}<input required type="number" min="0" step="any" className="border p-2 ml-2" value={durations[k][0]} onChange={e=>setDurations({...durations,[k]:[Number(e.target.value),durations[k][1]]})}/><select aria-label={`${k} duration unit`} value={durations[k][1]} onChange={e=>setDurations({...durations,[k]:[durations[k][0],e.target.value as DurationUnit]})}>{['seconds','minutes','hours'].map(u=><option key={u}>{u}</option>)}</select></label>)}
        <label className="block">Abort failure threshold (%)<input required className="border p-2 ml-2" type="number" min="0.01" max="100" step="0.01" value={percent} onChange={e=>setPercent(Number(e.target.value))}/></label>
        <button disabled={!account || devices.isLoading || groups.isLoading || groupDevices.isLoading || !!devices.error || !!groups.error || !!groupDevices.error}>Review rollout</button>
      </fieldset>
    </form>
    {complete && <p role="status">Rollout created. Use its panel below to approve admission. <button type="button" onClick={()=>setComplete(false)}>Create another rollout</button></p>}
    {error && <p role="alert">{error}</p>}
    {review && <section className="border p-4 space-y-2"><h3>Review exact rollout before signing</h3><p>{review.component} {review.version}</p><p>Recipe {review.recipe}</p><p className="break-all">Artifact SHA-256: {review.artifactSha256}<br/>Recipe SHA-256: {review.recipeSha256}</p>{review.cohorts.map((c,i)=><p key={i}>Cohort {i+1}: {c.map(id=>names.get(id) ?? id).join(', ')}</p>)}<p>Concurrency: {review.concurrency}; grants/window: {review.rateCount}; rate window: {review.rateWindowMs} ms; bake: {review.bakeMs} ms; minimum samples: {review.minimumSamples}; failure threshold: {review.failureBps} basis points ({review.failureBps/100}%); stall timeout: {review.offlineTimeoutMs} ms.</p><p>No devices admitted until you return here and approve admission.</p><button disabled={busy} onClick={()=>setReview(null)}>Edit</button><button disabled={busy || !account} onClick={async()=>{ try { await create(review); setReview(null); setComplete(true); } catch(e) {setError(String(e));} }}>Confirm creation in wallet</button></section>}
  </details>;
}
