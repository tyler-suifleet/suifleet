import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createRoot } from 'react-dom/client';
import FirmwarePage from '../../dapp/src/app/firmware/page';


const storageClient = {
  $extend: () => ({ walrus: {
    systemState: async () => ({ committee: { epoch: 35 }, future_accounting: { length: 54 } }),
    stakingState: async () => ({ epoch_duration: '86400000', first_epoch_start: String(Date.now() - 35 * 86400000) }),
    storageCost: async (size, epochs) => ({ totalCost: BigInt(size * epochs), storageCost: BigInt(size * epochs), writeCost: 0n }),
    getVerifiedBlobStatus: async () => ({ type: 'permanent', endEpoch: 42 }),
    getBlobType: () => '0x2::blob::Blob',
  } }),
  listOwnedObjects: async () => ({ objects: [], hasNextPage: false }),
};
const device = { objectId: '0x123', name: 'Acceptance device', arch: 'x86_64' };
export const useCurrentAccount = () => ({ address: '0xa' });
export const useDAppKit = () => ({});
export const useCurrentNetwork = () => 'testnet';
export const useCurrentClient = () => storageClient;
export const useDevices = () => ({ data: [device] });
export const useGroups = () => ({ data: [] });
export const useAccessPolicies = () => ({ data: [] });
const state = { requests: [], walletCalls: 0, resolve: null, reject: null, alerts: [] };
export const useFleetTransaction = () => ({ isPending: false, mutateAsync: ({ onProgress }) => {
  onProgress?.("validating");
  state.onProgress = onProgress;
  state.walletCalls++;
  return new Promise((resolve, reject) => { state.resolve = resolve; state.reject = reject; });
} });
class Request {
  upload = new EventTarget();
  open(method, url) { this.url = url; }
  setRequestHeader() {}
  send(body) { this.body = body; state.requests.push(this); }
}
window.XMLHttpRequest = Request;
window.alert = message => state.alerts.push(message);
window.uploadAcceptance = {
  count: () => state.requests.length,
  walletCalls: () => state.walletCalls,
  alerts: () => state.alerts,
  progress(index, loaded, total) {
    const event = new Event('progress');
    Object.assign(event, { loaded, total: total ?? 0, lengthComputable: total !== null });
    state.requests[index].upload.dispatchEvent(event);
  },
  sent(index) { state.requests[index].upload.dispatchEvent(new Event('load')); },
  finish(index, status = 200) {
    const request = state.requests[index];
    request.status = status;
    request.responseText = JSON.stringify({ newlyCreated: { blobObject: { blobId: `blob-${index}`, storage: { endEpoch: 35 + Number(new URL(request.url).searchParams.get("epochs")) } } } });
    request.onload();
  },
  phase(phase) { state.onProgress(phase); },
  reject() { state.reject(new Error('User rejected signing')); },
  succeed() { state.resolve({ digest: 'confirmed-fixture' }); },
};
createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient()}><FirmwarePage /></QueryClientProvider>);
