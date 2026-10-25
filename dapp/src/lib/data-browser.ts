import type { DataItem } from './opaque-data';
export type DataRow = { device:string; batch:string; item:number; atMs:string; value:DataItem };
export type DataFilters = { device:string; group:string; stream:string; key:string; contentType:string; from:string; to:string; search:string };
export const emptyDataFilters:DataFilters={device:'',group:'',stream:'',key:'',contentType:'',from:'',to:'',search:''};
export const rowId=(row:DataRow)=>`${row.device}/${row.batch}/${row.item}`;
export function filterData(rows:DataRow[],filters:DataFilters,groups:{objectId:string;deviceIds:string[]}[]) {
  const group=groups.find(g=>g.objectId===filters.group);
  const from=filters.from?Date.parse(filters.from):-Infinity,to=filters.to?Date.parse(filters.to):Infinity;
  if(Number.isNaN(from)||Number.isNaN(to)||from>to)throw Error('Choose a valid time range');
  return rows.filter(r=>(!filters.device||r.device===filters.device)&&(!filters.group||!!group?.deviceIds.includes(r.device))
    &&(!filters.stream||r.value.stream===filters.stream)&&r.value.key.toLowerCase().includes(filters.key.toLowerCase())
    &&r.value.content_type.toLowerCase().includes(filters.contentType.toLowerCase())&&Number(r.atMs)>=from&&Number(r.atMs)<=to
    &&(!filters.search||[r.device,r.value.stream,r.value.key,r.value.content_type,new TextDecoder().decode(new Uint8Array(r.value.metadata))].join(' ').toLowerCase().includes(filters.search.toLowerCase())));
}
export function textPreview(bytes:Uint8Array,contentType:string) {
  if(bytes.length>32768||!(/^(text\/|application\/(json|[^;]+\+json)(;|$))/i.test(contentType)))return undefined;
  try {const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);return contentType.split(';')[0].endsWith('json')?JSON.stringify(JSON.parse(text),null,2):text;}catch{return undefined;}
}
export function downloadName(row:DataRow) {return `${row.value.key.replace(/[^a-zA-Z0-9._-]/g,'_').slice(0,100)||'data'}-${row.value.upload.slice(0,12)}-chunk${row.value.chunk}`;}
export function exportUrl(input:string,name:string) {
  const url=new URL(input.replaceAll('{name}',encodeURIComponent(name)));
  if(url.protocol!=='https:'||url.username||url.password||url.hash)throw Error('Use an HTTPS upload URL without embedded credentials or fragment');
  return url.href;
}
export async function exportBytes(input:string,name:string,bytes:Uint8Array,contentType:string,token:string,signal:AbortSignal) {
  const response=await fetch(exportUrl(input,name),{method:'PUT',body:Uint8Array.from(bytes),headers:{'Content-Type':contentType,...(token?{Authorization:`Bearer ${token}`}:{})},
    credentials:'omit',redirect:'error',referrerPolicy:'no-referrer',signal:AbortSignal.any([signal,AbortSignal.timeout(60000)])});
  if(!response.ok)throw Error(`Export refused (${response.status}); check destination CORS and upload permission`);
}
