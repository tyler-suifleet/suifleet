import React, { useSyncExternalStore } from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import AdministrationPage from '../../dapp/src/app/handover/page';
import * as real from '../../dapp/src/lib/fleet-handover';
import {fleet_handover_ProposedBcs} from '../../dapp/src/lib/fleet-protocol';
import {moveType} from '../../dapp/src/lib/deployment-manifest';
const id=n=>'0x'+n.toString(16).padStart(64,'0');
let state={account:id(10),pending:false,bad:false}, listeners=new Set(), request;
window.admin={calls:[],update(changes){state={...state,...changes};listeners.forEach(f=>f());},release(){const finish=window.admin.finish;window.admin.finish=null;finish();},id};
const useState=()=>useSyncExternalStore(f=>{listeners.add(f);return()=>listeners.delete(f)},()=>state);
export const useCurrentAccount=()=>({address:useState().account});
export const useCurrentClient=()=>({});
export const useCurrentNetwork=()=> 'localnet';
const data=x=>({data:x,isLoading:false,error:null});
export const useDevices=()=>data([{objectId:id(30),ownerCapId:id(31),name:'Alpha Pi'}]);
export const useGroups=()=>data([{objectId:id(40),name:'Test lab',admin:id(10)}]);
export const useDeployments=()=>data([{deploymentId:id(50),creator:id(10),schemaVersion:'1',componentName:'Sensor app',version:'1.0.0'},{deploymentId:id(51),creator:id(10),schemaVersion:'2',componentName:'Catalog-only update',version:'2'}]);
export const buildProposeHandoverTx=real.buildProposeHandoverTx;
export const buildHandoverActionTx=real.buildHandoverActionTx;
export const buildHandoverStepsTx=real.buildHandoverStepsTx;
export const readHandover=async()=>request;
export function useFleetTransaction(){const f=useState();return {isPending:f.pending,mutate(tx,callbacks){
 window.admin.calls.push(tx.transaction.getData());window.admin.update({pending:true});
 window.admin.finish=()=>{try{
 const commands=tx.transaction.getData().commands; const operation=commands[0].MoveCall.function;
 if(operation==='propose'){
   request={source:id(10),recipient:id(11),objects:[id(31),id(40)],kinds:['0','1'],completed:[false,false],expires_ms:String(Date.now()+86400000),state:0};
   const event={...request,handover_id:id(60),source:f.bad?id(99):id(10)};
   callbacks.onSuccess({digest:'confirmed-fixture-request',events:[{eventType:moveType('fleet_handover::Handover').replace('::Handover','::Proposed'),bcs:fleet_handover_ProposedBcs.serialize(event).toBytes()}]});
 }else{ if(operation==='confirm')request.state=1;else if(operation==='finish')request.state=3;else request.completed=[true,true];callbacks.onSuccess({digest:'confirmed-fixture-action',events:[]});}
 }finally{window.admin.update({pending:false});callbacks.onSettled?.();}};
}};}
createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><AdministrationPage/></QueryClientProvider>);
