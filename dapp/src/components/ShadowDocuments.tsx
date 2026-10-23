"use client";
// Decrypted shadow documents from a verified schema-2 fleet archive (SF-084).
// Rendered as text only; the plaintext lives in page memory for the session.
import type { DocumentView, ShadowDocumentsView } from '@/lib/shadow-documents';
import { utc } from '@/lib/device-services';

const json = (value: unknown) => JSON.stringify(value, null, 2);

export function ShadowDocuments({view,index}:{view:ShadowDocumentsView;index?:number}) {
  if(view.state==='unavailable') return <section data-testid="shadow-documents"><h2 className="font-semibold">Shadow documents</h2><p role="status" className="text-sm">{view.reason}</p></section>;
  return <section className="space-y-2" data-testid="shadow-documents">
    <h2 className="font-semibold">Shadow documents</h2>
    <p className="text-sm" data-testid="shadow-documents-age">{view.newest
      ? `Current: the device's newest archive (#${index}, sequence ${view.seq}), signed ${utc(view.observedMs)}. Changes since then arrive with its next archive.`
      : `History: archive #${index} (sequence ${view.seq}), signed ${utc(view.observedMs)}. Newer archives exist; these are not the current documents.`}</p>
    {view.documents.length===0 && <p className="text-sm">The device had no shadows.</p>}
    {view.documents.map(d=><Document key={d.name} doc={d} />)}
    {view.omitted>0 && <p className="text-xs">{view.omitted} more shadows did not fit the archive's size limit and are not shown.</p>}
  </section>;
}

function Document({doc}:{doc:DocumentView}) {
  const title = doc.name==='' ? 'Classic shadow' : `Shadow ${doc.name}`;
  if(doc.state==='unavailable') return <div className="border-b pb-2"><p className="font-medium">{title}</p><p className="text-sm">Unavailable. {doc.reason}</p></div>;
  return <div className="border-b pb-2"><p className="font-medium">{title} · version {doc.version} · updated {utc(doc.updated_ms)}</p>
    <div className="grid md:grid-cols-3 gap-2 text-xs">
      {([['Desired',doc.desired],['Reported',doc.reported],['Delta',doc.delta]] as const).map(([label,value])=><div key={label}><p className="font-medium">{label}</p>
        <pre className="p-2 bg-gray-100 overflow-auto max-h-64 whitespace-pre-wrap">{value===null?'(none)':json(value)}</pre></div>)}
    </div></div>;
}
