"use client";
import { liveArchiveEntries } from "@/lib/stream-retention";
import { StreamRetentionPanel } from "@/components/StreamRetention";
import { useEffect, useRef, useState } from 'react';
import { useCurrentAccount, useCurrentClient, useDAppKit } from '@mysten/dapp-kit-react';
import { SessionKey } from '@mysten/seal';
import { normalizeSuiAddress as id } from '@mysten/sui/utils';
import { linkedDevice } from '@/lib/device-services';
import { Transaction } from '@mysten/sui/transactions';
import { useDevices } from '@/hooks/useDevices';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { DATA_CONFIG, readDeviceData, readFleetData, listFleetDeviceIds } from '@/lib/fleet-data';
import { archiveDecryptor, fetchVerifiedArchive, readAnchorEntries, parseCompactHead, type Anchor, type Archive } from '@/lib/fleet-bulk';
import { fetchBudgets } from '@/lib/fleet-budget';
import { requireManifest, verifyDeployment } from '@/lib/deployment-manifest';
import { SEAL_SETTINGS } from '@/lib/seal';
import { WALRUS_AGGREGATOR } from '@/lib/constants';
import { PageHeader } from '@/components/ui';
import { recordAccess, type AccessEntry, type LogQuery } from '@/lib/archive-search';
import { AccessRecord, ArchiveView } from '@/components/ArchiveSearch';
import { ShadowDocuments } from '@/components/ShadowDocuments';
import { shadowDocumentsView } from '@/lib/shadow-documents';

const noQuery:LogQuery={text:'',source:'',caseSensitive:false};

export default function FleetDataPage() {
  const client=useCurrentClient(), wallet=useDAppKit(), account=useCurrentAccount();
  const {data:devices=[]}=useDevices(); const {mutateAsync:execute,isPending}=useFleetTransaction();
  const [device,setDevice]=useState(''),[entries,setEntries]=useState<{index:number;anchor:Anchor}[]>([]);
  const [archive,setArchive]=useState<Archive>(),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  const [fleetDevices,setFleetDevices]=useState<string[]>([]),[cursor,setCursor]=useState<string|null>(null),[more,setMore]=useState(false);
  const [role,setRole]=useState<'owner'|'support'|'none'|'unknown'>('unknown'),[access,setAccess]=useState<AccessEntry[]>([]);
  const [query,setQuery]=useState<LogQuery>(noQuery),[opened,setOpened]=useState<number>(),[anchorCount,setAnchorCount]=useState(0);
  const session=useRef<SessionKey | undefined>(undefined), generation=useRef(0);
  // Discard the session with every plaintext derived from it, including a
  // search query that may hold copied log text. A pending read decrypted
  // under the discarded session no longer matches session.current.
  function expire(){session.current=undefined;setArchive(undefined);setOpened(undefined);setQuery(noQuery);setMessage('Seal session expired; decrypted records were discarded.');}
  useEffect(()=>{const linked=linkedDevice(new URLSearchParams(window.location.search).get('device'));if(linked)setDevice(linked);},[]);
  useEffect(()=>{generation.current++;session.current=undefined;setArchive(undefined);setOpened(undefined);setQuery(noQuery);setEntries([]);setAnchorCount(0);setMessage('');setBusy(false);},[account?.address,device,client]);
  // The access record and the signed session belong to one account.
  useEffect(()=>{setAccess([]);setRole('unknown');},[account?.address,client]);
  // Plaintext lives only as long as the signed five-minute Seal session.
  useEffect(()=>{
    const timer=setInterval(()=>{if(session.current?.isExpired())expire();},5_000);
    return ()=>clearInterval(timer);
  },[]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{
    let live=true;
    if(DATA_CONFIG && account)void authority().then(a=>{if(live)setRole(a.kind);}).catch(()=>{if(live)setRole('none');});
    return()=>{live=false;};
  },[account?.address,client]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{
    let live=true;setFleetDevices([]);setCursor(null);setMore(false);
    if(DATA_CONFIG && account)void listFleetDeviceIds(client,DATA_CONFIG).then(page=>{if(live){setFleetDevices(page.devices);setCursor(page.cursor);setMore(page.hasNextPage);}}).catch(()=>{if(live)setMessage('Fleet index unavailable.');});
    return()=>{live=false;};
  },[account?.address,client]);
  async function loadMore(){
    if(!DATA_CONFIG)return;const current=generation.current;
    try{const page=await listFleetDeviceIds(client,DATA_CONFIG,cursor);if(current===generation.current){setFleetDevices(old=>[...new Set([...old,...page.devices])]);setCursor(page.cursor);setMore(page.hasNextPage);}}
    catch{setMessage('Fleet index page unavailable.');}
  }
  async function authority() {
    if(!account || !DATA_CONFIG) throw Error('Connect a wallet to this fleet');
    const fleet=await readFleetData(client,DATA_CONFIG);
    const budgetId=DATA_CONFIG.budgetId;
    const owned=(await fetchBudgets(client,account.address)).find(b=>id(b.id)===budgetId);
    if(owned) return {kind:'owner' as const,ownerCapId:owned.capId};
    if(!fleet.support.some(a=>id(a)===id(account.address))) throw Error('This account has no archive read permission');
    return {kind:'support' as const};
  }
  async function refresh() {
    if(!DATA_CONFIG || !device)return;
    const current=generation.current;
    try {
      await authority();
      const writer=await readDeviceData(client,DATA_CONFIG,device);
      const count=Number(writer?.anchor_count ?? 0);
      if(!Number.isSafeInteger(count)) throw Error('Archive index exceeds browser limits');
      const start=Math.max(0,count-32),{entries:page}=await readAnchorEntries(client,DATA_CONFIG,device,start,32);
      const {walrus,TESTNET_WALRUS_PACKAGE_CONFIG}=await import('@mysten/walrus');
      const epoch=(await client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus.systemState()).committee.epoch;
      const live=await liveArchiveEntries(client,DATA_CONFIG,device,page,epoch);
      if(current===generation.current){setEntries(live.reverse());setAnchorCount(count);}
    } catch {if(current===generation.current){setArchive(undefined);setEntries([]);setAnchorCount(0);setMessage('Archive index unavailable or permission revoked.');}}
  }
  useEffect(()=>{void refresh();const timer=setInterval(()=>void refresh(),60_000);return ()=>clearInterval(timer);},[device,account?.address,client]); // eslint-disable-line react-hooks/exhaustive-deps
  function note(index:number,kind:number,authority:'owner'|'support'|undefined,outcome:'opened'|'refused') {
    if(!account || !authority)return;
    setAccess(log=>recordAccess(log,{atMs:Date.now(),account:account.address,authority,device,index,kind:(['logs','diagnostics','crash'] as const)[kind] ?? 'logs',outcome}));
  }
  function close(){setArchive(undefined);setOpened(undefined);setQuery(noQuery);}
  async function open(index:number,kind:number) {
    if(!DATA_CONFIG || !SEAL_SETTINGS || !account)return;
    const current=generation.current;setBusy(true);close();setMessage('');
    let allowed: Awaited<ReturnType<typeof authority>> | undefined;
    try {
      const manifest=requireManifest();await verifyDeployment(client);
      const connected=wallet.stores.$connection.get().account;
      if(connected?.address!==account.address || !connected.chains.includes(`sui:${manifest.network}`) || wallet.stores.$currentNetwork.get()!==manifest.network) throw Error('Wallet network/account mismatch');
      allowed=await authority();
      if(!session.current || session.current.isExpired()) {
        const key=await SessionKey.create({address:account.address,packageId:DATA_CONFIG.typeOrigin,ttlMin:5,suiClient:client});
        const signed=await wallet.signPersonalMessage({message:key.getPersonalMessage(),account:connected,network:manifest.network});
        await key.setPersonalMessageSignature(signed.signature);
        if(current!==generation.current || (wallet.stores.$connection.get().account?.address!==account.address || wallet.stores.$currentNetwork.get()!==manifest.network)) throw Error('Account or network changed');
        session.current=key;
      }
      // Plaintext is published only under the same unexpired session that decrypted it.
      const reading=session.current;
      const decrypt=await archiveDecryptor(client,DATA_CONFIG,SEAL_SETTINGS,reading,allowed);
      const {walrus,TESTNET_WALRUS_PACKAGE_CONFIG}=await import('@mysten/walrus');
      const storage=client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
      const opened=await fetchVerifiedArchive(client,storage,DATA_CONFIG,device,index,WALRUS_AGGREGATOR,decrypt);
      await authority();
      if(current!==generation.current)return;
      if(session.current!==reading || reading.isExpired()) {if(session.current===reading)expire();note(index,kind,allowed.kind,'refused');return;}
      if(wallet.stores.$connection.get().account?.address===account.address && wallet.stores.$currentNetwork.get()===manifest.network) {
        setArchive(opened);setOpened(index);note(index,kind,allowed.kind,'opened');
        if([3,4].includes(opened.schema) && opened.previous_head?.length){
          const previous=parseCompactHead(new Uint8Array(opened.previous_head));
          const previousIndex=Number(previous.index);
          if(Number.isSafeInteger(previousIndex)) {
            const epoch=(await storage.systemState()).committee.epoch;
            const live=await liveArchiveEntries(client,DATA_CONFIG,device,[{index:previousIndex,anchor:previous.anchor}],epoch);
            if(current===generation.current && session.current===reading && !reading.isExpired())setEntries(old=>old.some(e=>e.index===previousIndex)?old:[...old,...live]);
          }
        }
      }
    } catch {if(current===generation.current){note(index,kind,allowed?.kind,'refused');setMessage('Archive unavailable: identity, signature, policy, expiry, network or provider verification failed.');}}
    finally{if(current===generation.current)setBusy(false);}
  }
  async function diagnostics() {
    if(!DATA_CONFIG || !device)return;setMessage('');
    try {
      const allowed=await authority(), writer=await readDeviceData(client,DATA_CONFIG,device);
      if(!writer)throw Error('Device is not enrolled');
      const tx=new Transaction();
      if(allowed.kind==='owner') {
        tx.moveCall({target:`${DATA_CONFIG.packageId}::fleet_data::request_diagnostics`,arguments:[
          tx.object(DATA_CONFIG.fleetId),tx.object(writer.id),tx.object(DATA_CONFIG.budgetId),tx.object(allowed.ownerCapId),tx.object('0x6'),
        ]});
      } else {
        tx.moveCall({target:`${DATA_CONFIG.packageId}::fleet_data::request_diagnostics_support`,arguments:[
          tx.object(DATA_CONFIG.fleetId),tx.object(writer.id),tx.object('0x6'),
        ]});
      }
      await execute({transaction:tx});setMessage('Diagnostic request confirmed. The device will upload when online, within its emergency cadence and prepaid limits.');
    } catch {setMessage('Diagnostic request unavailable or refused.');}
  }
  return <div className="max-w-5xl space-y-4"><PageHeader title="Fleet archives" subtitle="Signed logs, diagnostics and shadow documents, verified from Sui and decrypted in this browser" />
    <StreamRetentionPanel />
    {!DATA_CONFIG ? <p>Direct-chain fleet data is not configured for this deployment.</p> : <>
      <label>Device <select className="border rounded p-2" value={device} onChange={e=>setDevice(e.target.value)}><option value="">Choose a device</option>{fleetDevices.map(device=><option key={device} value={device}>{devices.find(d=>id(d.objectId)===id(device))?.name ?? device}</option>)}</select></label>
      {more && <button className="border rounded p-2 ml-3" onClick={()=>void loadMore()}>Load more devices</button>}
      <button className="border rounded p-2 ml-3" disabled={!device || isPending || (role!=='owner' && role!=='support')} onClick={()=>void diagnostics()}>Request diagnostics</button>
      {!SEAL_SETTINGS && <p>Configure this deployment’s Seal providers to read archives.</p>}
      <p data-testid="archive-role">{role==='owner'?'This account holds the fleet budget owner capability and may decrypt archives.':role==='support'?'This account is a granted support account and may decrypt archives.':role==='none'?'This account is not authorized to decrypt fleet archives. Log search and diagnostic bundles are unavailable.':'Checking archive read permission…'}</p>
      {message && <p role="status">{message}</p>}
      {entries.length>0 && (role==='owner' || role==='support') && <button className="border rounded p-2" disabled={busy || !SEAL_SETTINGS} onClick={()=>void open(entries[0].index,entries[0].anchor.kind)}>Open current shadow documents</button>}
      <ul>{entries.map(({index,anchor})=><li key={index} className="border-b p-3"><button disabled={busy || !SEAL_SETTINGS} onClick={()=>void open(index,anchor.kind)} className="underline text-blue-700">{['Logs','Diagnostics','Crash'][anchor.kind]} · {new Date(Number(anchor.at_ms)).toLocaleString()}</button><span className="ml-3 text-sm">Originally purchased through Walrus epoch {anchor.end_epoch}; live retention is checked on open</span></li>)}</ul>
      {DATA_CONFIG?.compactHistoryOrigin && <p className="text-sm">Older archive links appear as you open their newer archive. History reads verify at most 64 links from the current root; expired or missing predecessors are unavailable.</p>}
      {archive && opened!==undefined && <ShadowDocuments view={shadowDocumentsView(archive,opened,anchorCount)} index={opened} />}
      {archive && <ArchiveView archive={archive} index={opened} query={query} setQuery={setQuery} close={close} />}
      {(role==='owner' || role==='support') && <AccessRecord entries={access} />}
    </>}
  </div>;
}
