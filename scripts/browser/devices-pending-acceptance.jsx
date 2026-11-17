import React, { useState, useSyncExternalStore } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import DevicesPage from '../../dapp/src/app/devices/page';
import { FleetTransactionError } from '../../dapp/src/lib/tx-feedback';
// Registrations land in `unindexed` while indexing lags; `index()` lists them.
let devices=[{objectId:'0xda',name:'Alpha Pi',deviceAddress:'0xa',arch:'aarch64',ownerCapId:'0xca'}];
// Executed-but-unlisted registrations survive a reload, like chain state.
const unindexed=JSON.parse(localStorage.getItem('fixture-unindexed')??'[]'),listeners=new Set();
const saveUnindexed=()=>localStorage.setItem('fixture-unindexed',JSON.stringify(unindexed));let signCalls=0,lagging=true,failNext=null;
const notify=()=>listeners.forEach(fn=>fn());
const subscribe=fn=>{listeners.add(fn);return()=>listeners.delete(fn);};
const query=data=>({data,isLoading:false,isSuccess:true,error:null,refetch:async()=>{}});
export const useCurrentAccount=()=>({address:'0xa'});
const client=new Proxy({}, {get:(_target,key)=>{throw Error(`Unexpected chain access in devices fixture: ${String(key)}`);}});
export const useCurrentClient=()=>client;
export const useCurrentNetwork=()=> 'localnet';
// `fixture-unavailable` (set before a reload) makes inventory reads fail, as when the node is unreachable.
const unavailable=localStorage.getItem('fixture-unavailable')==='1';
export const useDevices=()=>{const data=useSyncExternalStore(subscribe,()=>devices);return unavailable?{data:undefined,isLoading:false,isSuccess:false,error:new Error('Failed to fetch'),refetch:async()=>{}}:{data,isLoading:false,isSuccess:true,error:null,refetch:async()=>{notify();}};};
export const useGroups=()=>query([]);
export const useDeployments=()=>query([]);
export const useDesiredStates=()=>query({});
export const useTelemetryToken=()=>'';
export const setTelemetryToken=()=>{};
export const useFleetHealth=()=>query({});
export const useDeploymentStorage=()=>query(null);
// Registration executes at once; the device is listed only after `index()`.
export const useFleetTransaction=()=>{const [isPending,setPending]=useState(false);return {isPending,mutate:(input,{onSuccess,onError})=>{signCalls++;setPending(true);input.onProgress?.('validating');input.onProgress?.('wallet');setTimeout(()=>{
  if(failNext==='uncertain-executed'){failNext=null;const fd=new FormData(document.querySelector('form'));unindexed.push({objectId:`0xe${signCalls}`,name:fd.get('name'),deviceAddress:fd.get('deviceAddress'),arch:fd.get('arch'),ownerCapId:`0xf${signCalls}`});saveUnindexed();setPending(false);onError?.(new FleetTransactionError('uncertain',new TypeError('Failed to fetch')));return;}
  if(failNext){const stage=failNext;failNext=null;setPending(false);onError?.(new FleetTransactionError(stage,stage==='rejected'?Object.assign(new Error('User rejected the request'),{code:4001}):new TypeError('Failed to fetch')));return;}
  const form=document.querySelector('form');const fd=new FormData(form);
  const row={objectId:`0xd${signCalls}`,name:fd.get('name'),deviceAddress:fd.get('deviceAddress'),arch:fd.get('arch'),ownerCapId:`0xc${signCalls}`};
  if(lagging){unindexed.push(row);saveUnindexed();}else{devices=[...devices,row];notify();}
  setPending(false);onSuccess?.({digest:'fixture'});},50);}};};
window.acceptance={fail:stage=>{failNext=stage;},signCalls:()=>signCalls,index:()=>{lagging=false;devices=[...devices,...unindexed.splice(0)];saveUnindexed();notify();}};
// Device groups uses real queries; the provider lets that tab mount.
createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient()}><DevicesPage/></QueryClientProvider>);
