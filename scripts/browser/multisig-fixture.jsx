import React from 'react';
import {createRoot} from 'react-dom/client';
import {MultisigAdministrator} from '../../dapp/src/components/MultisigAdministrator';
import {manifestClient} from '../fixtures/manifest-client';
import {Ed25519Keypair} from '@mysten/sui/keypairs/ed25519';
import {MultiSigPublicKey} from '@mysten/sui/multisig';
import {Transaction} from '@mysten/sui/transactions';
import {toBase64, fromBase64} from '@mysten/sui/utils';
const m=JSON.parse(process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST), client=manifestClient(m);
const keys=[1,2,3].map(i=>Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(i)));
const config={keys:keys.map(k=>toBase64(k.getPublicKey().toSuiBytes())),weights:[1,1,1],threshold:2};
const multi=MultiSigPublicKey.fromPublicKeys({threshold:2,publicKeys:keys.map(k=>({publicKey:k.getPublicKey(),weight:1}))});
const account={address:keys[0].toSuiAddress(),chains:['sui:localnet']};
const state={account,signs:0,wrongBytes:false};
window.multisig={state,config};
export const useCurrentClient=()=>client;
export const useFleetTransaction=()=>({mutateAsync:async()=>{throw Error('fixture registration unavailable')}});
export const useDAppKit=()=>({stores:{$connection:{get:()=>({account:state.account})},$currentNetwork:{get:()=>m.network}},signTransaction:async({transaction})=>{
 state.signs++;const bytes=await transaction.build();const signed=await keys[0].signTransaction(bytes);return {...signed,bytes:state.wrongBytes?'changed':signed.bytes};
}});
(async()=>{
const tx=new Transaction();tx.setSender(multi.toSuiAddress());tx.setGasOwner(multi.toSuiAddress());tx.setGasPrice(1000);tx.setGasBudget(10000000);tx.setGasPayment([{objectId:'0xabc',version:'1',digest:'11111111111111111111111111111111'}]);tx.transferObjects([tx.splitCoins(tx.gas,[1])[0]],'0xb');
window.multisig.bundle=JSON.stringify({schema:1,chain:m.chain_identifier,packageId:m.call_package_id,customerId:'0xc',config,bytes:toBase64(await tx.build()),signatures:[]});
createRoot(document.getElementById('root')).render(<MultisigAdministrator customerId="0xc"/>);
})();
