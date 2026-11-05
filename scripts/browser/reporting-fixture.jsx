// UI boundary fixtures only; production resolver/PTBs are tested separately.
import React,{useState,useContext,createContext} from 'react';
import {createRoot} from 'react-dom/client';
import {FleetReportingSetup} from '../../dapp/src/components/FleetReportingSetup';
const Context=createContext(null);
let server,fail=true,enrolled=false,invalidRelease=false;
export function useDevices(){return {data:[{objectId:'device-1',name:'First device'}]};}
export function useCurrentClient(){return {};}
export function useFleetSelection(){return useContext(Context).selection;}
export function useFleetReporting(){return useContext(Context).query;}
export function useFleetTransaction(){return {isPending:false,mutateAsync:async({transaction})=>{
 if(fail){fail=false;throw Error('User rejected the request');}
 if(transaction==='setup')server={fleetId:'verified-data'};
 if(transaction==='enroll')enrolled=true;
 return {digest:'fixture-confirmed-digest'};
}};}
export function reportingRelease(){if(invalidRelease)throw Error('Invalid release');return {packageId:'fixture'};}
export async function buildProvisionReporting(){return 'setup';}
export async function buildEnrollReporting(){return 'enroll';}
export async function reportingClientSettings(){if(!enrolled)throw Error('Enroll device reporting before downloading');return '[fleet_data]\nfleet_id = "verified-data"\n';}
function Harness(){
 const [checking,setChecking]=useState(true),[data,setData]=useState(),[error,setError]=useState(null),[admin,setAdmin]=useState(true),[identity,setIdentity]=useState(1);
 const refresh=async()=>{setChecking(false);setData(server);setError(null);return {data:server};};
 const fleet={id:`fleet-${identity}`,customerId:'customer',devices:['device-1'],administrator:admin};
 const query={data,error,isPending:checking,isError:!!error,isFetching:false,refetch:refresh};
 return <Context.Provider value={{selection:{fleet},query}}>
  <button onClick={()=>setError(Error('Transport unavailable'))}>Simulate unavailable readiness</button>
  <button onClick={()=>{invalidRelease=true;setError(Error("Invalid release"));}}>Break reporting release</button>
  <button onClick={()=>setAdmin(false)}>Revoke administrator</button>
  <button onClick={()=>{server=undefined;enrolled=false;setData(undefined);setIdentity(v=>v+1);}}>Select second fleet</button>
  <FleetReportingSetup key={identity}/>
 </Context.Provider>;
}
createRoot(document.getElementById('root')).render(<Harness/>);
