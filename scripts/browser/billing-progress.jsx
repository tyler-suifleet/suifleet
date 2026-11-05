// Rendering fixture only: no wallet, credentials or network transactions.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import BillingPage from '../../dapp/src/app/billing/page';
export * from '../../dapp/src/lib/subscription';
const id = '0x' + '1'.repeat(64);
export function useCurrentAccount() { return { address: id }; }
export function useCurrentClient() { return {}; }
export function useFleetSelection() { return { identity: 'fixture', fleet: { customerId: id, administrator: true }, refresh() {} }; }
export const quotes = () => [{ offerId: id, symbol: 'SUI', source: 'fixture', observedAt: Date.now(), usdPerCoin: 1 }];
export const treasuryLabel = async () => 'Test treasury';
export const readOffer = async () => ({ offer: { tier: 1, device_limit: 3, allowance: 1, expires_at_ms: '1999999999999' }, annual: false, usd: 1, total: 1000000000n, descriptor: { decimals: 9 } });
export const customerAccount = async () => ({ symbol: 'SUI', account: { id, tier: 1, device_limit: 3, term_end_ms: '1999999999999', next_due_ms: '0', balance: { value: '1000000000' }, remaining: '1', cancelled: false } });
export const history = async () => [];
export const warnings = () => [];
let release;
const pause = () => new Promise(resolve => { release = resolve; });
window.billing = { calls: 0, release: () => release(), fail: false };
export const preparePrepay = async () => { await pause(); return {}; };
export const collectTx = () => ({});
export const verifyPlan = async () => {};
export function useFleetTransaction() { return { isPending: false, mutateAsync: async ({ onProgress, onSubmitted }) => {
  window.billing.calls++;
  onProgress('validating'); await pause();
  onProgress('wallet'); await pause();
  onProgress('confirming'); onSubmitted('fixture-digest'); await pause();
  if (window.billing.fail) throw Error('Confirmation unavailable');
  return { digest: 'fixture-digest' };
} }; }
createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient()}><BillingPage /></QueryClientProvider>);
