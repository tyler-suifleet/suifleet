"use client";
import { useEffect, useRef, useState } from 'react';
import { useCurrentAccount, useCurrentClient, useDAppKit } from '@mysten/dapp-kit-react';
import { SessionKey } from '@mysten/seal';
import { normalizeSuiAddress as id } from '@mysten/sui/utils';
import { Transaction } from '@mysten/sui/transactions';
import { useDevices } from '@/hooks/useDevices';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { DATA_CONFIG, readDeviceData, readFleetData, listFleetDeviceIds } from '@/lib/fleet-data';
import { archiveDecryptor, fetchVerifiedArchive, readAnchors, type Anchor, type Archive } from '@/lib/fleet-bulk';
import { fetchBudgets } from '@/lib/fleet-budget';
import { requireManifest, verifyDeployment } from '@/lib/deployment-manifest';
import { SEAL_SETTINGS } from '@/lib/seal';
import { WALRUS_AGGREGATOR } from '@/lib/constants';
import { PageHeader } from '@/components/ui';

export default function FleetDataPage() {
  const client=useCurrentClient(), wallet=useDAppKit(), account=useCurrentAccount();
  const {data:devices=[]}=useDevices(); const {mutateAsync:execute,isPending}=useFleetTransaction();
  const [device,setDevice]=useState(''),[entries,setEntries]=useState<{index:number;anchor:Anchor}[]>([]);
  const [archive,setArchive]=useState<Archive>(),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  const [fleetDevices,setFleetDevices]=useState<string[]>([]),[cursor,setCursor]=useState<string|null>(null),[more,setMore]=useState(false);
  const session=useRef<SessionKey | undefined>(undefined), generation=useRef(0);
  useEffect(()=>{generation.current++;session.current=undefined;setArchive(undefined);setEntries([]);setMessage('');setBusy(false);},[account?.address,device,client]);
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
      const start=Math.max(0,count-32),anchors=await readAnchors(client,DATA_CONFIG,device,start,32);
      if(current===generation.current) setEntries(anchors.map((anchor,i)=>({index:start+i,anchor})).reverse());
    } catch {if(current===generation.current){setArchive(undefined);setEntries([]);setMessage('Archive index unavailable or permission revoked.');}}
  }
  useEffect(()=>{void refresh();const timer=setInterval(()=>void refresh(),30_000);return ()=>clearInterval(timer);},[device,account?.address,client]); // eslint-disable-line react-hooks/exhaustive-deps
  async function open(index:number) {
    if(!DATA_CONFIG || !SEAL_SETTINGS || !account)return;
    const current=generation.current;setBusy(true);setArchive(undefined);setMessage('');
    try {
      const manifest=requireManifest();await verifyDeployment(client);
      const connected=wallet.stores.$connection.get().account;
      if(connected?.address!==account.address || !connected.chains.includes(`sui:${manifest.network}`) || wallet.stores.$currentNetwork.get()!==manifest.network) throw Error('Wallet network/account mismatch');
      const allowed=await authority();
      if(!session.current || session.current.isExpired()) {
        const key=await SessionKey.create({address:account.address,packageId:DATA_CONFIG.typeOrigin,ttlMin:5,suiClient:client});
        const signed=await wallet.signPersonalMessage({message:key.getPersonalMessage(),account:connected,network:manifest.network});
        await key.setPersonalMessageSignature(signed.signature);
        if(current!==generation.current || (wallet.stores.$connection.get().account?.address!==account.address || wallet.stores.$currentNetwork.get()!==manifest.network)) throw Error('Account or network changed');
        session.current=key;
      }
      const decrypt=await archiveDecryptor(client,DATA_CONFIG,SEAL_SETTINGS,session.current,allowed);
      const {walrus,TESTNET_WALRUS_PACKAGE_CONFIG}=await import('@mysten/walrus');
      const storage=client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
      const opened=await fetchVerifiedArchive(client,storage,DATA_CONFIG,device,index,WALRUS_AGGREGATOR,decrypt);
      await authority();
      if(current===generation.current && wallet.stores.$connection.get().account?.address===account.address && wallet.stores.$currentNetwork.get()===manifest.network) setArchive(opened);
    } catch {if(current===generation.current)setMessage('Archive unavailable: identity, signature, policy, expiry, network or provider verification failed.');}
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
  return <div className="max-w-5xl space-y-4"><PageHeader title="Fleet archives" subtitle="Signed logs and diagnostics, verified from Sui and decrypted in this browser" />
    {!DATA_CONFIG ? <p>Direct-chain fleet data is not configured for this deployment.</p> : <>
      <label>Device <select className="border rounded p-2" value={device} onChange={e=>setDevice(e.target.value)}><option value="">Choose a device</option>{fleetDevices.map(device=><option key={device} value={device}>{devices.find(d=>id(d.objectId)===id(device))?.name ?? device}</option>)}</select></label>
      {more && <button className="border rounded p-2 ml-3" onClick={()=>void loadMore()}>Load more devices</button>}
      <button className="border rounded p-2 ml-3" disabled={!device || isPending} onClick={()=>void diagnostics()}>Request diagnostics</button>
      {!SEAL_SETTINGS && <p>Configure this deployment’s Seal providers to read archives.</p>}
      {message && <p role="status">{message}</p>}
      <ul>{entries.map(({index,anchor})=><li key={index} className="border-b p-3"><button disabled={busy || !SEAL_SETTINGS} onClick={()=>void open(index)} className="underline text-blue-700">{['Logs','Diagnostics','Crash'][anchor.kind]} · {new Date(Number(anchor.at_ms)).toLocaleString()}</button><span className="ml-3 text-sm">Expires at Walrus epoch {anchor.end_epoch}</span></li>)}</ul>
      {archive && <section><p>{archive.records.length} verified records; {archive.dropped_lines} dropped lines reported by device.</p><pre className="p-3 bg-gray-100 overflow-auto max-h-[36rem] whitespace-pre-wrap">{archive.records.map(r=>`[${r.source}] ${r.line}`).join('\n')}</pre></section>}
    </>}
  </div>;
}
