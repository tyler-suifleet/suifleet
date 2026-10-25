export { buildCreateCustomerTx, buildCreateFleetTx, selectionKey, selectFleet } from "../../dapp/src/lib/customer-fleets";
export type { NamedFleet } from "../../dapp/src/lib/customer-fleets";
import type { NamedFleet } from "../../dapp/src/lib/customer-fleets";
import { state, A, deviceIds } from "./customer-wallet-browser";
export const F="0x"+"f".repeat(64), G="0x"+"2".repeat(64), H="0x"+"3".repeat(64);
export const customersSupported = () => true;
export async function fetchCustomerFleets(_client: unknown, wallet: string, warn?: (message: string) => void): Promise<NamedFleet[]> {
 const make=(id:string,name:string,index:number):NamedFleet=>({id,customerId:wallet===A?"0xc":"0xb",customerName:wallet===A?"Acme":"Other customer",name,budgetId:"0x"+String(index+4).repeat(64),devices:[deviceIds[index]],groups:[],deployments:[],policies:[],enrollmentPolicies:[],credentials:[],administrator:true,isDefault:index===0 || index===2,activeDevices:2n,deviceLimit:10n,allowance:100n,used:6n});
 const result=state.revoked?[]:wallet===A?[make(F,"Production",0),make(G,"Testing",1),...(state.created?[make(H,"Experiments",1)]:[])]:[make(H,"Separate fleet",2)];
 if(state.delayed)await new Promise<void>(resolve=>state.pending.push(resolve));
 if(state.fail)throw Error("Discovery unavailable");
 if(state.warning)warn?.("Reader/support discovery unavailable for another fleet.");
 return result;
}
