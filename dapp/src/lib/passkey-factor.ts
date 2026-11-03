import { ObjectError } from '@mysten/sui/client';
import { bcs } from '@mysten/sui/bcs';
import { BrowserPasskeyProvider, PasskeyKeypair, PasskeyPublicKey, type PasskeyProvider } from '@mysten/sui/keypairs/passkey';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction } from '@mysten/sui/transactions';
import { fromBase64, toBase64, normalizeSuiAddress, normalizeStructTag } from '@mysten/sui/utils';
import { MANIFEST, moveType, verifyDeployment } from './deployment-manifest';
import { PACKAGE_ID } from './constants';
import { security_policy_FactorKeyBcs, security_policy_FactorConfigurationBcs } from './fleet-protocol';

export interface SavedPasskey { publicKey: string; credentialId: string }
export const PASSKEY_ENROLLMENT_TYPE = 'passkey_factor::Enrollment';
export function passkeySupported() { return !!MANIFEST?.type_origins[PASSKEY_ENROLLMENT_TYPE]; }
function storageKey() { return `suifleet-passkeys-v1:${MANIFEST?.chain_identifier}:${location.origin}`; }
export function savedPasskeys(): SavedPasskey[] {
  const parsed: unknown = JSON.parse(localStorage.getItem(storageKey()) ?? '[]');
  if (!Array.isArray(parsed) || parsed.length > 64) throw Error('Invalid saved passkey metadata');
  return parsed.map(p => {
    if (!p || typeof p.publicKey !== 'string' || typeof p.credentialId !== 'string') throw Error('Invalid saved passkey metadata');
    new PasskeyPublicKey(fromBase64(p.publicKey));
    return { publicKey: p.publicKey, credentialId: p.credentialId };
  });
}
export function preferPasskey(key: SavedPasskey) {
  localStorage.setItem(storageKey(), JSON.stringify([key, ...savedPasskeys().filter(k => k.publicKey !== key.publicKey)]));
}
export function exportPasskeys() { return JSON.stringify(savedPasskeys()); }
export function importPasskeys(value: string) {
  const previous = localStorage.getItem(storageKey());
  try { localStorage.setItem(storageKey(), value); savedPasskeys(); }
  catch (e) { if (previous === null) localStorage.removeItem(storageKey()); else localStorage.setItem(storageKey(), previous); throw e; }
}
export function passkeyAddress(key: SavedPasskey) { return new PasskeyPublicKey(fromBase64(key.publicKey)).toSuiAddress(); }
function provider(credentialId?: string): PasskeyProvider {
  if (!globalThis.isSecureContext || !navigator.credentials || !globalThis.PublicKeyCredential) throw Error('Passkeys require HTTPS (or localhost), WebAuthn and a supported authenticator');
  const base = new BrowserPasskeyProvider('SuiFleet administrator', { rp: { id: location.hostname }, authenticatorSelection: { userVerification: 'required', residentKey: 'required' } });
  return {
    create: () => base.create(),
    get: async challenge => {
      const result = await navigator.credentials.get({ publicKey: { challenge: Uint8Array.from(challenge), rpId: location.hostname, userVerification: 'required', timeout: 60000,
        ...(credentialId ? { allowCredentials: [{ type: 'public-key' as const, id: Uint8Array.from(fromBase64(credentialId)) }] } : {}) } });
      if (!result) throw Error('Passkey authentication was cancelled');
      return result as Awaited<ReturnType<PasskeyProvider['get']>>;
    },
  };
}
export async function createPasskey(): Promise<SavedPasskey> {
  const base = provider(); let credentialId = '';
  const signer = await PasskeyKeypair.getPasskeyInstance({ ...base, create: async () => {
    const credential = await base.create(); credentialId = toBase64(new Uint8Array(credential.rawId)); return credential;
  } });
  const key = { publicKey: toBase64(signer.getPublicKey().toRawBytes()), credentialId };
  // Public metadata only. Keeping metadata before on-chain enrollment permits
  // interrupted setup to resume; on-chain registration remains authoritative.
  localStorage.setItem(storageKey(), JSON.stringify([...savedPasskeys(), key]));
  return key;
}
export function passkeySigner(key: SavedPasskey) { return new PasskeyKeypair(fromBase64(key.publicKey), provider(key.credentialId)); }
export async function fetchPasskeyEntries(client: SuiGrpcClient, customerId: string): Promise<string[]> {
  await verifyDeployment(client);
  if (!passkeySupported()) return [];
  try {
    const { dynamicField } = await client.getDynamicField({ parentId: customerId, name: { type: moveType('security_policy::FactorKey'), bcs: security_policy_FactorKeyBcs.serialize({ factor: 2 }).toBytes() } });
    if (normalizeStructTag(dynamicField.value.type) !== normalizeStructTag(moveType('security_policy::FactorConfiguration'))) throw Error('Unexpected passkey configuration type');
    const { binding } = security_policy_FactorConfigurationBcs.parse(dynamicField.value.bcs);
    const entries = bcs.vector(bcs.Address).parse(Uint8Array.from(binding));
    if (entries.length % 2 || entries.length > 64) throw Error('Invalid passkey configuration');
    return entries;
  } catch (e) {
    // Only a genuine missing field is an unenrolled factor. Transport and BCS
    // failures must remain errors; do not silently downgrade a required factor.
    if (e instanceof ObjectError && e.reason === 'notFound') return [];
    throw e;
  }
}
export async function enrolledSigner(client: SuiGrpcClient, customer: string, administrator: string) {
  const entries = await fetchPasskeyEntries(client, customer);
  const enrolled = new Set(entries.filter((_, i) => i % 2 === 1 && normalizeSuiAddress(entries[i - 1]) === normalizeSuiAddress(administrator)).map(v => normalizeSuiAddress(v)));
  const key = savedPasskeys().find(k => enrolled.has(normalizeSuiAddress(passkeyAddress(k))));
  if (!key) throw Error('No enrolled passkey is available in this browser. Restore its public credential metadata or use an independently enrolled recovery passkey. The wallet alone cannot reset protection.');
  return passkeySigner(key);
}
export function fundPasskeyTx(key: SavedPasskey) {
  const tx = new Transaction(); const [gas] = tx.splitCoins(tx.gas, [tx.pure.u64(10_000_000)]);
  tx.transferObjects([gas], tx.pure.address(passkeyAddress(key))); return tx;
}
export function enrollmentProofTx(customer: string, administrator: string, key: SavedPasskey, expires: bigint) {
  if (!passkeySupported()) throw Error('This deployment does not support passkey enrollment');
  const tx = new Transaction(); tx.setSender(passkeyAddress(key));
  tx.moveCall({ target: `${PACKAGE_ID}::passkey_factor::prove_enrollment`, arguments: [tx.object(customer), tx.pure.address(administrator), tx.pure.vector('u8', fromBase64(key.publicKey)), tx.pure.u64(expires), tx.object('0x6')] });
  return tx;
}
export function enrollPasskeyTx(customer: string, proof: string) {
  const tx = new Transaction(); tx.moveCall({ target: `${PACKAGE_ID}::passkey_factor::enroll`, arguments: [tx.object(customer), tx.object(proof), tx.object('0x6')] }); return tx;
}
export function removePasskeyTx(customer: string, administrator: string, key: string) {
  const tx = new Transaction(); tx.moveCall({ target: `${PACKAGE_ID}::passkey_factor::remove`, arguments: [tx.object(customer), tx.pure.address(administrator), tx.pure.address(key)] }); return tx;
}

// These calls authenticate administrators but do not execute protected actions.
// Exempt only a PTB consisting entirely of current-package approval decisions.
export function approvalDecisionsOnly(transaction: Transaction) {
  const commands = transaction.getData().commands;
  return commands.length > 0 && commands.every(command => {
    const call = command.MoveCall;
    return !!call && normalizeSuiAddress(call.package) === normalizeSuiAddress(PACKAGE_ID) &&
      call.module === 'customer' && ['approve_action', 'reject_action', 'cancel_disclosed_security_action'].includes(call.function);
  });
}

// Keep SDK gas-backed intents for wallet-paid transactions. At the sponsorship
// boundary, copy the PTB and select sender-owned SUI for application funding.
// Direct GasCoin arguments cannot be safely rewritten and remain refused.
export function withSponsorFunding(transaction: Transaction): Transaction {
  const data = transaction.getData();
  for (const command of data.commands) {
    if (command.$Intent?.name === 'CoinWithBalance' && command.$Intent.data.type === 'gas') {
      command.$Intent.data.type = normalizeSuiAddress('0x2') + '::sui::SUI';
    }
  }
  const sponsored = Transaction.from(JSON.stringify(data, (_key, value) => typeof value === 'bigint' ? String(value) : value));
  assertSponsorSafe(sponsored);
  return sponsored;
}

// Sponsorship changes tx.gas ownership. Never let application value come from
// the passkey's payment coin, including unresolved SDK gas-balance intents.
export function assertSponsorSafe(transaction: Transaction) {
  const commands = transaction.getData().commands;
  const usesGas = (value: unknown): boolean => {
    if (!value || typeof value !== 'object') return false;
    if ('$kind' in value && value.$kind === 'GasCoin') return true;
    return Object.values(value).some(usesGas);
  };
  if (usesGas(commands) || commands.some(c => c.$Intent?.data.type === 'gas')) {
    throw Error('Passkey sponsorship requires wallet-owned funding coins. Rebuild this transaction without GasCoin before signing.');
  }
}
