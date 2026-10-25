// Offline component fixture. Real scoped envelope/signature verification and AES;
// indexed metadata, DEM keys and grants are explicit synthetic boundaries.
import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {EncryptedObject} from '@mysten/seal';
import {Transaction} from '@mysten/sui/transactions';
import {DataBrowser} from '@/components/DataBrowser';
import {openDataItem,assembleDataItems,setReadGrant} from '@/lib/opaque-data';
const f=DATA_FIXTURE;
let page=0,deny=false,corrupt=false,delay=false;
const buffers=[],grants=[];
window.fixtureState={buffers,grants,pending:0};
async function open(row){window.fixtureState.pending++;try{return await rawOpen(row);}finally{window.fixtureState.pending--;}}
async function rawOpen(row){
 const x=f.fixtures.find(x=>x.row.device===row.device&&x.row.batch===row.batch);
 const ciphertext=Uint8Array.from(x.ciphertext);if(corrupt)ciphertext[ciphertext.length-1]^=1;
 const bytes=await openDataItem(ciphertext,f.config,row.device,x.anchor,row.value,async encrypted=>{
   if(deny)throw Error('Access denied: scoped read grant unavailable');
   const {blob,aad}=EncryptedObject.parse(encrypted).ciphertext.Aes256Gcm;
   const key=await crypto.subtle.importKey('raw',Uint8Array.from(x.demKey),'AES-GCM',false,['decrypt']);
   return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:Uint8Array.from([138,55,153,253,198,46,121,219,160,128,89,7,214,156,148,220]),additionalData:aad},key,blob));
 });
 buffers.push(bytes);if(delay)await new Promise(r=>setTimeout(r,150));return bytes;
}
const adapter={page:async()=>({rows:page++===0?f.fixtures.map(x=>x.row):[],more:page<2}),open,
 upload:async rows=>{const parts=[];try{for(const row of rows)parts.push({index:row.value,bytes:await open(row)});return assembleDataItems(parts);}finally{parts.forEach(p=>p.bytes.fill(0));}},
 grant:async(device,stream,reader,allowed)=>{const tx=new Transaction();await setReadGrant(tx,f.config,'0xc1',device,stream,reader,allowed);grants.push({device,stream,reader,allowed,commands:tx.getData().commands});},
};
function Harness(){const [scope,setScope]=useState(0),[owner,setOwner]=useState(true);
 return <><button onClick={()=>{page=0;setScope(n=>n+1);}}>Change account/network or expire session</button>
 <button onClick={()=>setOwner(false)}>Reader role</button>
 <label><input type="checkbox" onChange={e=>deny=e.target.checked} /> Deny keys</label>
 <label><input type="checkbox" onChange={e=>corrupt=e.target.checked} /> Corrupt ciphertext</label>
 <label><input type="checkbox" onChange={e=>delay=e.target.checked} /> Delay open</label>
 <DataBrowser adapter={adapter} devices={f.devices} groups={f.groups} owner={owner} connected scope={String(scope)} />
 </>;
}
createRoot(document.getElementById('root')).render(<Harness/>);
