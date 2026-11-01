import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DeviceEntitlements } from "../../dapp/src/components/DeviceEntitlements";
import { manifestClient } from "../fixtures/manifest-client";
import { requireManifest } from "../../dapp/src/lib/deployment-manifest";
import { customer_CustomerBcs, device_registry_DeviceRegistryBcs } from "../../dapp/src/lib/fleet-protocol";
const id=n=>"0x"+n.toString(16).padStart(64,"0"), m=requireManifest(), C=id(12), A=id(10);
const admin=new URLSearchParams(location.search).has("admin");
const client=manifestClient(m), original=client.getObject;
let current={id:C,version:"1",registry_id:m.shared_objects.registry.id,name:"Test customer",credentials:{contents:[]},administrators:"1",fleets:[],default_fleet:null,active_devices:"0",device_limit:"0",allowance:"25",used:"10",period:"7",charge_type:[]};
client.getObject=async input=>{
 if(input.objectId===C)return {object:{type:`${m.call_package_id}::customer::Customer`,content:customer_CustomerBcs.serialize(current).toBytes()}};
 const result=await original(input);
 if(input.objectId===m.shared_objects.registry.id)result.object.content=device_registry_DeviceRegistryBcs.serialize({id:input.objectId,admin:A,device_count:"0",schema_version:"1"}).toBytes();
 return result;
};
export function useCurrentAccount(){return {address:admin?A:id(11)};}
export function useCurrentClient(){return client;}
export function useFleetSelection(){return {fleet:{customerId:C,customerName:"Test customer",activeDevices:0n,deviceLimit:0n},refresh(){}};}
export function useFleetTransaction(){return {mutateAsync:async({transaction})=>{window.entitlements.calls.push(transaction.getData());},isPending:false};}
window.entitlements={calls:[],customer:C};
createRoot(document.getElementById("root")).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><DeviceEntitlements /></QueryClientProvider>);
