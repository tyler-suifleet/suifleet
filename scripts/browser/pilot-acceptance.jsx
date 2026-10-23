import React from 'react';
import { createRoot } from 'react-dom/client';
import FleetDataPage from '../../dapp/src/app/data/page';
import { FleetDataBcs, DATA_CONFIG } from '../../dapp/src/lib/fleet-data';
import { bcs } from '@mysten/sui/bcs';
import { normalizeSuiAddress as id, toBase58 } from '@mysten/sui/utils';

// Only transport and wallet hooks are fixtures. Real page and index verifier run.
const account = { address: id('0xc0') };
const state = { fail: false, pages: 0 };
const devices = Array.from({ length: 128 }, (_,i) => id('0x'+(4096+i).toString(16)));
const client = {
 ledgerService: { async getServiceInfo() { return { response: {chainId: toBase58(Uint8Array.from([0x4c,0x78,0xad,0xac,...Array(28).fill(0)]))} }; } },
 async getObject() { return {object:{owner:{$kind:'Shared'},type:`${DATA_CONFIG.typeOrigin}::fleet_data::FleetData`,content:FleetDataBcs.serialize({id:DATA_CONFIG.fleetId,version:'1',budget_id:DATA_CONFIG.budgetId,storage_id:DATA_CONFIG.storageId,registry_id:DATA_CONFIG.registryId,chain_identifier:DATA_CONFIG.chainIdentifier,key_epoch:'0',support:[],health_interval_ms:'3600000',batch_interval_ms:'86400000',emergency_interval_ms:'300000',device_count:'128'}).toBytes()}}; },
 async listDynamicFields({cursor,limit}) {
  state.pages++;if(state.fail)throw Error('fixture RPC throttling');
  if(limit!==100)throw Error('unexpected page bound');
  const start=cursor?Number(cursor):0, end=Math.min(start+limit,devices.length);
  return {dynamicFields:devices.slice(start,end).map(device=>({$kind:'DynamicField',name:{type:`${DATA_CONFIG.typeOrigin}::fleet_data::DeviceKey`,bcs:bcs.Address.serialize(device).toBytes()},valueType:'0x2::object::ID'})),hasNextPage:end<devices.length,cursor:end<devices.length?String(end):null};
 }
};
export const useCurrentAccount = () => account;
export const useCurrentClient = () => client;
export const useDAppKit = () => ({});
export const useDevices = () => ({data:[]});
export const useFleetTransaction = () => ({isPending:false,mutateAsync:async()=>{throw Error('No writes authorized by browser fixture');}});
window.pilotAcceptance = {fail: value=>{state.fail=value;}, pages:()=>state.pages};
createRoot(document.getElementById('root')).render(<FleetDataPage/>);
