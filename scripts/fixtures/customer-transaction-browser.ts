import { state } from "./customer-wallet-browser";
export function useFleetTransaction() { return { isPending:false, mutateAsync:async ({transaction}:{transaction:{getData():unknown}})=>{
 state.transactions.push(transaction.getData());state.created=true;return {digest:"fixture-only"};
} }; }
