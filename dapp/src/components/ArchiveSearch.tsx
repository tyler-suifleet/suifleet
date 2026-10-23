"use client";
// Verified archive log search and the session access record (SF-084).
import { archiveSources, searchArchive, type AccessEntry, type LogQuery } from '@/lib/archive-search';
import type { Archive } from '@/lib/fleet-bulk';

export function ArchiveView({archive,index,query,setQuery,close}:{archive:Archive;index?:number;query:LogQuery;setQuery:(q:LogQuery)=>void;close:()=>void}) {
  let result: ReturnType<typeof searchArchive> | undefined, error='';
  try{result=searchArchive(archive,query);}catch(e){error=e instanceof Error?e.message:String(e);}
  return <section className="space-y-2" data-testid="archive-view">
    <p>Archive #{index} · {archive.reason} · sequence {archive.seq} · {archive.records.length} verified records; {archive.dropped_lines} dropped lines reported by device.</p>
    <div className="flex flex-wrap gap-3 items-center">
      <label>Search <input className="border rounded p-1" maxLength={256} value={query.text} onChange={e=>setQuery({...query,text:e.target.value})} /></label>
      <label>Source <select className="border rounded p-1" value={query.source} onChange={e=>setQuery({...query,source:e.target.value})}><option value="">All sources</option>{archiveSources(archive).map(s=><option key={s} value={s}>{s}</option>)}</select></label>
      <label><input type="checkbox" checked={query.caseSensitive} onChange={e=>setQuery({...query,caseSensitive:e.target.checked})} /> Match case</label>
      <button className="border rounded p-1" onClick={close}>Close and discard</button>
    </div>
    {error ? <p role="alert">{error}</p> : result && <>
      <p data-testid="search-count">{result.matches.length}{result.truncated?'+':''} of {archive.records.length} records match{result.truncated?' (first 1000 shown)':''}.</p>
      <pre className="p-3 bg-gray-100 overflow-auto max-h-[36rem] whitespace-pre-wrap">{result.matches.map(r=>`${r.index+1}\t[${r.source}] ${r.line}`).join('\n')}</pre>
    </>}
  </section>;
}

export function AccessRecord({entries}:{entries:AccessEntry[]}) {
  return <section className="space-y-1" data-testid="access-record"><h2 className="font-semibold">Archive access in this session</h2>
    <p className="text-sm">Each open is recorded here with the account and authority used; each Seal key request carries the signed session. Diagnostic requests are recorded on chain. This list stays in this tab’s memory and is never stored or sent.</p>
    {entries.length===0 ? <p className="text-sm">No archives opened.</p> : <ul className="text-sm font-mono">{entries.map((e,i)=><li key={i}>{new Date(e.atMs).toISOString()} · {e.outcome} · {e.authority} {e.account} · device {e.device} archive #{e.index} ({e.kind})</li>)}</ul>}
  </section>;
}
