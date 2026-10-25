"use client";
import { useEffect, useRef, useState } from 'react';
import { emptyDataFilters, filterData, rowId, textPreview, downloadName, exportBytes, type DataRow } from '@/lib/data-browser';

export type DataBrowserAdapter={
  page:()=>Promise<{rows:DataRow[];more:boolean}>;
  open:(row:DataRow,signal?:AbortSignal)=>Promise<Uint8Array>;
  upload:(rows:DataRow[],signal?:AbortSignal)=>Promise<Uint8Array>;
  grant:(device:string,stream:string|undefined,reader:string,allowed:boolean)=>Promise<void>;
};
export function DataBrowser({adapter,devices,groups,owner,connected,scope}:{adapter:DataBrowserAdapter;devices:{id:string;name:string}[];groups:{objectId:string;name:string;deviceIds:string[]}[];owner:boolean;connected:boolean;scope:string}) {
  const [rows,setRows]=useState<DataRow[]>([]),[more,setMore]=useState(true),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const [filters,setFilters]=useState(emptyDataFilters),[selected,setSelected]=useState<DataRow>(),[opened,setOpened]=useState<{row:DataRow;bytes:Uint8Array;complete?:boolean}>();
  const [reader,setReader]=useState(''),[wholeDevice,setWholeDevice]=useState(false),[url,setUrl]=useState(''),[token,setToken]=useState('');
  const generation=useRef(0),plain=useRef<Uint8Array|undefined>(undefined),active=useRef(false),controller=useRef<AbortController|undefined>(undefined);
  function discard(){plain.current?.fill(0);plain.current=undefined;setOpened(undefined);}
  useEffect(()=>{generation.current++;active.current=false;controller.current?.abort();discard();setRows([]);setSelected(undefined);setFilters(emptyDataFilters);setMore(true);setBusy(false);setMessage('');setUrl('');setToken('');setReader('');
    return()=>{generation.current++;controller.current?.abort();plain.current?.fill(0);};
  },[scope]); // eslint-disable-line react-hooks/exhaustive-deps
  async function run(work:(current:number,signal:AbortSignal)=>Promise<void>){
    if(active.current)return;active.current=true;setBusy(true);setMessage('');const current=generation.current;
    const abort=new AbortController();controller.current=abort;
    try{await work(current,abort.signal);}catch(e){if(current===generation.current)setMessage(e instanceof Error?e.message:'Data unavailable');}
    finally{if(current===generation.current){active.current=false;setBusy(false);}}
  }
  async function load(){await run(async current=>{const page=await adapter.page();if(current!==generation.current)return;
    setRows(old=>{const combined=new Map(old.map(r=>[rowId(r),r]));for(const r of page.rows)combined.set(rowId(r),r);return [...combined.values()].slice(0,512);});
    setMore(page.more);setMessage(page.rows.length?'Metadata page loaded.':'No items in this page; legacy fields or devices without generic data may still have more history.');
  });}
  async function open(row:DataRow){discard();setSelected(row);await run(async(current,signal)=>{const bytes=await adapter.open(row,signal);
    if(current!==generation.current){bytes.fill(0);return;}plain.current=bytes;setOpened({row,bytes});
  });}
  async function openUpload(row:DataRow){discard();await run(async(current,signal)=>{
    const parts=rows.filter(r=>r.device===row.device&&r.value.upload===row.value.upload&&r.value.stream===row.value.stream);
    if(parts.length!==row.value.chunks)throw Error('Complete upload not loaded; load more metadata pages first');
    const bytes=await adapter.upload(parts,signal);if(current!==generation.current){bytes.fill(0);return;}plain.current=bytes;setOpened({row,bytes,complete:true});
  });}
  function download(){if(!opened)return;const blob=new Blob([Uint8Array.from(opened.bytes)],{type:'application/octet-stream'}),link=document.createElement('a');
    link.href=URL.createObjectURL(blob);link.download=downloadName(opened.row)+(opened.complete?'-complete':'');link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);
  }
  let visible:DataRow[]=[],filterError='';try{visible=filterData(rows,filters,groups);}catch(e){filterError=(e as Error).message;}
  const preview=opened?textPreview(opened.bytes,opened.row.value.content_type):undefined;
  async function exportSet(all:boolean){await run(async(current,signal)=>{
    const targets=all?visible:opened?[opened.row]:[];
    if(!targets.length)throw Error('Open an item or choose a nonempty filtered set');
    if(targets.length>16||targets.reduce((n,r)=>n+Number(r.value.size),0)>64*1024*1024)throw Error('Export at most 16 chunks / 64 MiB at a time');
    if(targets.length>1&&!url.includes('{name}'))throw Error('For multiple items use an upload URL containing {name}');
    let sent=0;
    for(const row of targets){if(signal.aborted||current!==generation.current)return;
      const bytes=await adapter.open(row,signal);
      try{if(signal.aborted||current!==generation.current)return;
        await exportBytes(url,`${row.device}-${row.batch}-${row.item}-${downloadName(row)}`,bytes,row.value.content_type,token,signal);sent++;
      }finally{bytes.fill(0);}
    }
    if(current===generation.current)setMessage(`Exported ${sent} verified chunks. Destination storage is customer-controlled.`);
  });}
  return <section className="space-y-4 border rounded p-4" aria-label="Device data browser">
    <h2 className="text-xl font-semibold">Device data</h2>
    <p>Browse public metadata across the fleet, then decrypt only the items you need. Metadata, sizes and plaintext SHA-256 hashes are public and can reveal predictable content. Legacy logs below remain fleet-wide.</p>
    {!connected&&<p>Connect a wallet to decrypt or share data. Public metadata can still be browsed.</p>}
    <div className="flex flex-wrap gap-3">
      <label>Search metadata <input value={filters.search} maxLength={256} onChange={e=>setFilters({...filters,search:e.target.value})} className="border p-1" /></label>
      <label>Data device <select aria-label="Data device" value={filters.device} onChange={e=>setFilters({...filters,device:e.target.value})}><option value="">All devices</option>{[...new Set([...devices.map(d=>d.id),...rows.map(r=>r.device)])].map(d=><option key={d} value={d}>{devices.find(x=>x.id===d)?.name||d}</option>)}</select></label>
      <label>Group <select aria-label="Group" value={filters.group} onChange={e=>setFilters({...filters,group:e.target.value})}><option value="">All groups</option>{groups.map(g=><option key={g.objectId} value={g.objectId}>{g.name}</option>)}</select></label>
      <label>Stream <select aria-label="Stream" value={filters.stream} onChange={e=>setFilters({...filters,stream:e.target.value})}><option value="">All streams</option>{[...new Set(rows.map(r=>r.value.stream))].sort().map(s=><option key={s}>{s}</option>)}</select></label>
      <label>Key <input value={filters.key} maxLength={256} onChange={e=>setFilters({...filters,key:e.target.value})} className="border p-1" /></label>
      <label>Content type <input value={filters.contentType} maxLength={128} onChange={e=>setFilters({...filters,contentType:e.target.value})} className="border p-1" /></label>
      <label>From <input type="datetime-local" value={filters.from} onChange={e=>setFilters({...filters,from:e.target.value})} /></label>
      <label>To <input type="datetime-local" value={filters.to} onChange={e=>setFilters({...filters,to:e.target.value})} /></label>
      <button onClick={()=>setFilters(emptyDataFilters)}>Clear filters</button>
    </div>
    <p>Filters apply to {rows.length} loaded chunks, using chain inclusion time. Each click reads one bounded device-index page or item page. Up to 512 chunks / 256 devices per visit; narrow your fleet or reload to restart. Group membership is the current wallet-visible membership, not historical membership.</p>
    <button className="border rounded p-2" disabled={busy||!more||rows.length>=512} onClick={()=>void load()}>{busy?'Working…':rows.length?'Load next metadata page':'Browse fleet data'}</button>
    {filterError&&<p role="alert">{filterError}</p>}
    <p role="status">{message}</p>
    {!rows.length&&!busy&&<p>No generic data loaded. Browse the fleet index to begin; logs and diagnostics are available below.</p>}
    {rows.length>0&&!visible.length&&<p>No loaded items match these filters. More history may be available.</p>}
    {!more&&<p>End of available indexed metadata for this visit. Unpublished or unavailable history cannot be recovered here.</p>}
    <ul>{visible.map(row=><li key={rowId(row)} className="border-b p-2"><button className="underline" disabled={busy} onClick={()=>{discard();setSelected(row);}}>{row.value.key} · {row.value.stream}</button>
      <p>{devices.find(d=>d.id===row.device)?.name||row.device} · {row.value.content_type} · {row.value.size} bytes · chunk {row.value.chunk+1}/{row.value.chunks} · {new Date(Number(row.atMs)).toLocaleString()}</p>
    </li>)}</ul>
    {selected&&<section aria-label="Data item details" className="space-y-2 break-all">
      <h3 className="font-semibold">{selected.value.key} / {selected.value.stream}</h3>
      <p>Device {selected.device} · Groups: {groups.filter(g=>g.deviceIds.includes(selected.device)).map(g=>g.name).join(', ')||'None visible'}</p>
      <p>Upload {selected.value.upload} · batch {selected.batch} · sequence {selected.value.sequence} · chunk {selected.value.chunk+1} of {selected.value.chunks} · total {selected.value.total_size} bytes</p>
      <p>SHA-256 {Array.from(selected.value.sha256,b=>b.toString(16).padStart(2,'0')).join('')}</p>
      <pre className="whitespace-pre-wrap">{new TextDecoder().decode(new Uint8Array(selected.value.metadata))}</pre>
      <button disabled={busy||!connected} className="border p-2" onClick={()=>void open(selected)}>Decrypt and verify chunk</button>
      {selected.value.chunks>1&&<button disabled={busy||!connected} onClick={()=>void openUpload(selected)}>Verify complete upload</button>}
      {selected.value.chunks>1&&<p>Load every chunk to verify the complete upload. Chunk downloads and exports preserve chunk boundaries; incomplete uploads are never presented as complete files.</p>}
      {owner&&<fieldset disabled={busy}><legend>Scoped sharing</legend><label>Reader address <input value={reader} maxLength={66} onChange={e=>setReader(e.target.value)} className="border p-1" /></label>
        <label><input type="checkbox" checked={wholeDevice} onChange={e=>setWholeDevice(e.target.checked)} /> All streams on this device</label>
        <p>{wholeDevice?'Device-wide':`Stream ${selected.value.stream}`} access. Revocation cannot recall plaintext or keys already obtained. Epoch rotation denies older keys, including owner reads.</p>
        {[true,false].map(allowed=><button key={String(allowed)} className="border p-2" onClick={()=>void run(async current=>{await adapter.grant(selected.device,wholeDevice?undefined:selected.value.stream,reader,allowed);if(current===generation.current){discard();setMessage(allowed?'Read grant confirmed.':'Read grant revoked. Previously obtained keys and plaintext remain accessible.');}})}>{allowed?'Grant read access':'Revoke read access'}</button>)}
      </fieldset>}
    </section>}
    {opened&&<section aria-label="Verified data" className="space-y-2"><p>Verified {opened.complete?'complete upload':'plaintext chunk'} · {opened.bytes.length} bytes</p>
      {preview!==undefined?<pre className="max-h-96 overflow-auto whitespace-pre-wrap" data-testid="data-preview">{preview}</pre>:<p>Preview unavailable (binary, invalid UTF-8/JSON, or over 32 KiB). Download verified bytes instead.</p>}
      <button className="border p-2" onClick={download}>{opened.complete?'Download complete upload':'Download verified chunk'}</button><button className="border p-2" onClick={discard}>Discard plaintext</button>
    </section>}
    <fieldset disabled={busy||!connected} className="space-y-2"><legend>Export to your storage</legend>
      <p>Direct HTTPS PUT to your presigned S3-compatible or HTTP destination. Configure CORS for this site and the content type. For a filtered set use {'{name}'} in the URL; every chunk gets a unique name. Export re-verifies each chunk. Partial destination uploads may remain after a failure.</p>
      <label>Upload URL <input type="password" autoComplete="off" value={url} onChange={e=>setUrl(e.target.value)} className="border p-1" /></label>
      <label>Optional bearer token <input type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)} className="border p-1" /></label>
      <p>Credentials stay in this tab’s memory until cleared or the account/network changes. Nothing is saved to browser storage.</p>
      <button disabled={!opened||!url} onClick={()=>void exportSet(false)}>Export opened chunk</button>
      <button disabled={!visible.length||!url||!!filterError} onClick={()=>void exportSet(true)}>Export filtered chunks</button>
      <button onClick={()=>{setUrl('');setToken('');}}>Clear export credentials</button>
    </fieldset>
  </section>;
}
