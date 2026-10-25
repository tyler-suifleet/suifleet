"use client";
import { useEffect, useMemo, useRef, useState } from 'react';
import { useCurrentAccount, useCurrentClient, useDAppKit, useCurrentNetwork } from '@mysten/dapp-kit-react';
import { SessionKey } from '@mysten/seal';
import { Transaction } from '@mysten/sui/transactions';
import { isValidSuiAddress, normalizeSuiAddress as id, toHex } from '@mysten/sui/utils';
import { useDevices } from '@/hooks/useDevices';
import { useGroups } from '@/hooks/useGroups';
import { useFleetTransaction } from '@/hooks/useFleetTransaction';
import { listFleetDeviceIds, readFleetData } from '@/lib/fleet-data';
import { listDataItems, readDataItem, DataItemBcs, scopedDecryptor, fetchDataItem, assembleDataItems, setReadGrant } from '@/lib/opaque-data';
import { readAnchors } from '@/lib/fleet-bulk';
import { useFleetDataConfig } from '@/hooks/useFleetDataConfig';
import { useFleetSelection } from '@/hooks/useFleetSelection';
import { fetchCustomerFleets } from '@/lib/customer-fleets';
import { selectedDataAuthority } from '@/lib/data-authority';
import { fetchManagedBudget, fetchBudgets } from '@/lib/fleet-budget';
import { requireManifest, verifyDeployment } from '@/lib/deployment-manifest';
import { SEAL_SETTINGS } from '@/lib/seal';
import { WALRUS_AGGREGATOR } from '@/lib/constants';
import { DataBrowser, type DataBrowserAdapter } from './DataBrowser';
import { dataPages } from '@/lib/data-pages';
import type { DataRow } from '@/lib/data-browser';

export function FleetDataBrowser(){
  const DATA_CONFIG=useFleetDataConfig(),selection=useFleetSelection();
  const client=useCurrentClient(),account=useCurrentAccount(),wallet=useDAppKit(),network=useCurrentNetwork();
  const {data:devices=[]}=useDevices(),{data:groups=[],isError:groupError}=useGroups(),{mutateAsync:execute}=useFleetTransaction();
  const [owner,setOwner]=useState(false),[expired,setExpired]=useState(0);
  const session=useRef<SessionKey|undefined>(undefined),version=useRef(0);
  const scope=`${selection.identity}/${DATA_CONFIG?.fleetId}/${account?.address}/${network}/${expired}`;
  useEffect(()=>{version.current++;session.current=undefined;setOwner(false);
    let live=true;
    if(DATA_CONFIG&&account)void authority().then(a=>{if(live)setOwner(a.kind==='owner'||a.kind==='customer');}).catch(()=>{});
    return()=>{live=false;version.current++;session.current=undefined;};
  },[client,scope]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{const timer=setInterval(()=>{if(session.current?.isExpired()){session.current=undefined;setExpired(n=>n+1);}},1000);return()=>clearInterval(timer);},[]);
  async function authority(){
      if(!DATA_CONFIG||!account)throw Error('Connect a wallet');
      await readFleetData(client,DATA_CONFIG);
      if(selection.supported){
        return selectedDataAuthority(await fetchCustomerFleets(client,account.address),selection.fleet,DATA_CONFIG.budgetId);
      }
      const budget=(await fetchBudgets(client,account.address)).find(b=>id(b.id)===DATA_CONFIG!.budgetId);
      return budget?{kind:'owner' as const,ownerCapId:budget.capId}:{kind:'reader' as const};
    }
  const adapter=useMemo<DataBrowserAdapter>(()=>{
    async function check(){
      if(!DATA_CONFIG||!account)throw Error('Connect a wallet to read data');
      const manifest=requireManifest();await verifyDeployment(client);
      const current=wallet.stores.$connection.get().account;
      if(current?.address!==account.address||!current.chains.includes(`sui:${manifest.network}`)||wallet.stores.$currentNetwork.get()!==manifest.network)throw Error('Wallet account or network mismatch');
      return {manifest,current};
    }
    return {
      page:dataPages(async cursor=>{
        if(!DATA_CONFIG)throw Error('Fleet data is not configured');
        return listFleetDeviceIds(client,DATA_CONFIG,cursor);
      },async(device,cursor)=>{
        if(!DATA_CONFIG)throw Error('Fleet data is not configured');
        const page=await listDataItems(client,DATA_CONFIG,device,cursor),rows:DataRow[]=[];
        for(const entry of page.items){
          const start=Number(entry.value.anchor_index);
          if(!Number.isSafeInteger(start))throw Error('Data anchor exceeds browser limits');
          const [anchor]=await readAnchors(client,DATA_CONFIG,device,start,1);
          if(!anchor||anchor.seq!==entry.batch)throw Error('Data timestamp/anchor unavailable');
          rows.push({device,batch:entry.batch,item:entry.item,atMs:anchor.at_ms,value:entry.value});
        }
        return {rows,cursor:page.cursor,hasNextPage:page.hasNextPage};
      }),
      async open(row,signal){
        signal?.throwIfAborted();
        if(!DATA_CONFIG||!SEAL_SETTINGS)throw Error('Configure fleet data and Seal providers before decrypting');
        const currentVersion=version.current;const {manifest,current}=await check();
        const allowed=await authority();signal?.throwIfAborted();
        if(!session.current||session.current.isExpired()){
          const key=await SessionKey.create({address:account!.address,packageId:DATA_CONFIG.typeOrigin,ttlMin:5,suiClient:client});
          signal?.throwIfAborted();
          const signed=await wallet.signPersonalMessage({message:key.getPersonalMessage(),account:current,network:manifest.network});
          await key.setPersonalMessageSignature(signed.signature);await check();signal?.throwIfAborted();
          if(version.current!==currentVersion)throw Error('Account, network or session changed');session.current=key;
        }
        const reading=session.current;
        const index=await readDataItem(client,DATA_CONFIG,row.device,row.batch,row.item);
        if(toHex(DataItemBcs.serialize(index).toBytes())!==toHex(DataItemBcs.serialize(row.value).toBytes()))throw Error('Indexed metadata changed; reload');
        const decrypt=await scopedDecryptor(client,DATA_CONFIG,SEAL_SETTINGS,reading,row.device,row.value.stream,allowed);
        const {walrus,TESTNET_WALRUS_PACKAGE_CONFIG}=await import('@mysten/walrus');
        const storage=client.$extend(walrus({packageConfig:TESTNET_WALRUS_PACKAGE_CONFIG})).walrus;
        const bytes=await fetchDataItem(client,storage,DATA_CONFIG,row.device,row.batch,row.item,WALRUS_AGGREGATOR,decrypt);
        try{await check();const fresh=await authority();if(fresh.kind!==allowed.kind)throw Error('Fleet authority changed');signal?.throwIfAborted();if(version.current!==currentVersion||session.current!==reading||reading.isExpired())throw Error('Seal session changed or expired');return bytes;}
        catch(e){bytes.fill(0);throw e;}
      },
      async upload(rows,signal){
        signal?.throwIfAborted();
        if(!rows.length||rows.length>512)throw Error('Upload exceeds this visit limit');
        const first=rows[0];
        if(rows.length!==first.value.chunks||rows.some(r=>r.device!==first.device||r.value.upload!==first.value.upload||r.value.stream!==first.value.stream))throw Error('Incomplete or mixed upload');
        const parts:{index:DataRow['value'];bytes:Uint8Array}[]=[];
        try{for(const row of rows)parts.push({index:row.value,bytes:await this.open(row,signal)});signal?.throwIfAborted();
          const result=assembleDataItems(parts);if(session.current?.isExpired()){result.fill(0);throw Error('Seal session expired');}return result;}
        finally{for(const part of parts)part.bytes.fill(0);}
      },
      async grant(device,stream,reader,allowed){
        if(!DATA_CONFIG)throw Error('Fleet data is not configured');
        await check();if(!isValidSuiAddress(reader))throw Error('Enter a valid reader Sui address');
        const auth=await authority();if(auth.kind!=='owner'&&auth.kind!=='customer')throw Error('Only the current budget owner can change read grants');
        const capId=auth.kind==='customer'?(await fetchManagedBudget(client,DATA_CONFIG.budgetId)).capId:auth.ownerCapId;
        const tx=new Transaction();await setReadGrant(tx,DATA_CONFIG,capId,device,stream,reader,allowed);await execute({transaction:tx});
      },
    };
  },[client,account?.address,network,expired,execute,wallet,DATA_CONFIG,selection.identity]); // eslint-disable-line react-hooks/exhaustive-deps
  if(!DATA_CONFIG)return null;
  return <>{groupError&&<p role="status">Group details unavailable; group filters require current wallet-visible group reads.</p>}
    <DataBrowser key={scope} adapter={adapter} devices={devices.map(d=>({id:id(d.objectId),name:d.name}))} groups={groupError?[]:groups} owner={owner} connected={!!account} scope={scope} />
  </>;
}
