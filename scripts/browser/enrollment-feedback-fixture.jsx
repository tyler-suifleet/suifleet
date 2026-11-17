// SF-277 enrollment fixture: the real Enrollment page, job storage, planner,
// credential archive and transaction builders. Only chain reads (policy and
// claim snapshot), inventory hooks and wallet approval are scripted.
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as real from '../../dapp/src/lib/enrollment';
import { FleetTransactionError } from '../../dapp/src/lib/tx-feedback';
import { ALIAS_META_KEY, SERIAL_META_KEY } from '../../dapp/src/lib/sui';
import { REGISTRY_ID } from '../../dapp/src/lib/constants';
import { normalizeSuiAddress } from '@mysten/sui/utils';
import EnrollmentPage from '../../dapp/src/app/enrollment/page';

export * from '../../dapp/src/lib/enrollment';
const id = n => normalizeSuiAddress('0x' + n.toString(16));
const operator = id(10), policyId = id(100);
const policy = { id: policyId, operator, registry: normalizeSuiAddress(REGISTRY_ID), claimsTable: id(101), unusedClaims: 2, remaining: 10n, expiresAtMs: 0n, open: true, enrolled: 1n, version: '5' };
const device = { objectId: id(302), name: 'pi-2', chainName: 'pi-2', serial: 'SN-2', meta: { [ALIAS_META_KEY]: 'pi-2', [SERIAL_META_KEY]: 'SN-2' }, deviceAddress: id(502), arch: 'aarch64', registeredAt: 0, ownerCapId: id(402) };
// Claims registered on chain; a confirmed revocation removes them.
const registered = new Set([id(201)]);
const enrollments = new Map([[id(202), { policyId, deviceId: device.objectId, deviceAddress: device.deviceAddress, claim: id(202), operator, ownerCap: device.ownerCapId }]]);

if (!localStorage.getItem('fixture-seeded')) {
  const job = real.newJob(operator, policyId, 1_000_000n);
  job.entries = [
    { serial: 'SN-1', alias: 'pi-1', claim: id(201), submitted: true, observedRegistered: true, observedPolicyVersion: '5' },
    { serial: 'SN-2', alias: 'pi-2', claim: id(202), submitted: true, observedRegistered: true, observedPolicyVersion: '5' },
    { serial: 'SN-3', alias: 'pi-3', claim: id(203) },
  ];
  real.saveJob(localStorage, job);
  localStorage.setItem('fixture-seeded', '1');
}
// Revocations confirmed before a reload stay confirmed.
for (const claim of JSON.parse(localStorage.getItem('fixture-revoked') ?? '[]')) registered.delete(claim);

export const fetchPolicies = async () => [policy];
export const snapshot = async (_client, job) => ({
  policy, devices: [device], groups: [], enrollments,
  registered: new Set(job.entries.map(e => e.claim).filter(c => registered.has(c))),
  balances: new Map([...registered].map(c => [c, 10_000_000n])),
});

export const useCurrentAccount = () => ({ address: operator });
export const useCurrentClient = () => ({});
export const useCurrentNetwork = () => 'localnet';
export const useFleetSelection = () => ({ supported: false, identity: 'legacy', fleet: undefined, fleets: [] });
const query = data => ({ data, isLoading: false, isSuccess: true, error: null, refetch: async () => ({ data }) });
export const useDevices = () => query([device]);
export const useGroups = () => query([]);

window.fx = { calls: [], fail: null, pending: null, release() { const next = window.fx.pending; window.fx.pending = null; next(); } };
export function useFleetTransaction() {
  const [isPending, setPending] = useState(false);
  return { isPending, mutateAsync: ({ transaction, onProgress, onDefiniteFailure }) => {
    const data = transaction.getData();
    window.fx.calls.push(data.commands.map(c => c.MoveCall?.function));
    setPending(true); onProgress?.('wallet');
    return new Promise((resolve, reject) => { window.fx.pending = () => {
      setPending(false);
      const stage = window.fx.fail; window.fx.fail = null;
      if (stage) {
        if (stage !== 'uncertain') onDefiniteFailure?.();
        return reject(new FleetTransactionError(stage, stage === 'rejected' ? Object.assign(new Error('User rejected the request'), { code: 4001 }) : new TypeError('Failed to fetch')));
      }
      for (const command of data.commands) if (command.MoveCall?.function === 'revoke_claim') {
        const bytes = Uint8Array.from(atob(data.inputs[command.MoveCall.arguments[1].Input].Pure.bytes), c => c.charCodeAt(0));
        const claim = normalizeSuiAddress('0x' + [...bytes].map(b => b.toString(16).padStart(2, '0')).join(''));
        registered.delete(claim);
        localStorage.setItem('fixture-revoked', JSON.stringify([...JSON.parse(localStorage.getItem('fixture-revoked') ?? '[]'), claim]));
      }
      resolve({ digest: 'fixture-digest', events: [] });
    }; });
  } };
}

createRoot(document.getElementById('root')).render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><EnrollmentPage /></QueryClientProvider>);
