import React, { useState, useSyncExternalStore } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import DevicesPage from '../../dapp/src/app/devices/page';
// Registrations land in `unindexed` while indexing lags; `index()` lists them.
let devices=[{objectId:'0xda',name:'Alpha Pi',deviceAddress:'0xa',arch:'aarch64',ownerCapId:'0xca'}];
const unindexed=[],listeners=new Set();let signCalls=0,lagging=true;
const notify=()=>listeners.forEach(fn=>fn());
const subscribe=fn=>{listeners.add(fn);return()=>listeners.delete(fn);};
const query=data=>({data,isLoading:false,isSuccess:true,error:null,refetch:async()=>{}});
export const useCurrentAccount=()=>({address:'0xa'});
const client=new Proxy({}, {get:(_target,key)=>{throw Error(`Unexpected chain access in devices fixture: ${String(key)}`);}});
export const useCurrentClient=()=>client;
export const useCurrentNetwork=()=> 'localnet';
export const useDevices=()=>{const data=useSyncExternalStore(subscribe,()=>devices);return {data,isLoading:false,isSuccess:true,error:null,refetch:async()=>{notify();}};};
export const useGroups=()=>query([]);
export const useDeployments=()=>query([]);
export const useDesiredStates=()=>query({});
export const useTelemetryToken=()=>'';
export const setTelemetryToken=()=>{};
export const useFleetHealth=()=>query({});
export const useDeploymentStorage=()=>query(null);
// Registration executes at once; the device is listed only after `index()`.
export const useFleetTransaction=()=>{const [isPending,setPending]=useState(false);return {isPending,mutate:(_input,{onSuccess})=>{signCalls++;setPending(true);setTimeout(()=>{
  const form=document.querySelector('form');const fd=new FormData(form);
  const row={objectId:`0xd${signCalls}`,name:fd.get('name'),deviceAddress:fd.get('deviceAddress'),arch:fd.get('arch'),ownerCapId:`0xc${signCalls}`};
  if(lagging)unindexed.push(row);else{devices=[...devices,row];notify();}
  setPending(false);onSuccess?.({digest:'fixture'});},50);}};};
window.acceptance={signCalls:()=>signCalls,index:()=>{lagging=false;devices=[...devices,...unindexed.splice(0)];notify();}};
// Device groups uses real queries; the provider lets that tab mount.
createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient()}><DevicesPage/></QueryClientProvider>);
