import { bcs } from "@mysten/sui/bcs";
import { MultiSigPublicKey } from "@mysten/sui/multisig";
import { Transaction } from "@mysten/sui/transactions";
import { publicKeyFromSuiBytes, verifyTransactionSignature } from "@mysten/sui/verify";
import { fromBase64, toBase64, normalizeSuiAddress } from "@mysten/sui/utils";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { PACKAGE_ID } from "./constants";
import { requireManifest, verifyDeployment, moveType } from "./deployment-manifest";
import { security_policy_FactorKeyBcs, security_policy_FactorConfigurationBcs } from "./fleet-protocol";

export interface MultisigConfiguration { keys: string[]; weights: number[]; threshold: number }
const Configuration = bcs.struct("MultisigConfiguration", { sender: bcs.Address, keys: bcs.vector(bcs.vector(bcs.u8())), weights: bcs.vector(bcs.u8()), threshold: bcs.u16() });
export function multisigKey(config: MultisigConfiguration) {
  if (!Array.isArray(config.keys) || config.keys.length < 1 || config.keys.length > 10 || config.weights.length !== config.keys.length ||
      !Number.isInteger(config.threshold) || config.threshold < 1 || config.threshold > 65535 || config.weights.some(w => !Number.isInteger(w) || w < 1 || w > 255)) throw Error("Invalid multisig members or threshold");
  const publicKeys = config.keys.map((key, i) => {
    const bytes = fromBase64(key);
    if (![0,1,2,6].includes(bytes[0])) throw Error("Use an Ed25519, secp256k1, secp256r1 or passkey member");
    return { publicKey: publicKeyFromSuiBytes(bytes), weight: config.weights[i] };
  });
  return MultiSigPublicKey.fromPublicKeys({ threshold: config.threshold, publicKeys });
}
export async function fetchMultisig(client: SuiGrpcClient, customerId: string): Promise<MultisigConfiguration> {
  await verifyDeployment(client);
  const { dynamicField } = await client.getDynamicField({ parentId: customerId, name: { type: moveType("security_policy::FactorKey"), bcs: security_policy_FactorKeyBcs.serialize({ factor: 1 }).toBytes() } });
  if (dynamicField.value.type !== moveType("security_policy::FactorConfiguration")) throw Error("Unexpected multisig configuration type");
  const outer = security_policy_FactorConfigurationBcs.parse(dynamicField.value.bcs);
  const parsed = Configuration.parse(Uint8Array.from(outer.binding));
  const config = { keys: parsed.keys.map(k => toBase64(Uint8Array.from(k))), weights: parsed.weights, threshold: parsed.threshold };
  if (multisigKey(config).toSuiAddress() !== normalizeSuiAddress(parsed.sender)) throw Error("Multisig configuration address mismatch");
  return config;
}
export function enrollMultisigTx(customerId: string, config: MultisigConfiguration) {
  multisigKey(config);
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::multisig_admin::enroll`, arguments: [tx.object(customerId), tx.pure(bcs.vector(bcs.vector(bcs.u8())).serialize(config.keys.map(k => [...fromBase64(k)]))), tx.pure.vector("u8", config.weights), tx.pure.u16(config.threshold)] });
  return tx;
}
export function approveMultisigTx(customerId: string, actionId: string, config: MultisigConfiguration) {
  multisigKey(config);
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::multisig_admin::approve`, arguments: [tx.object(customerId), tx.object(actionId), tx.pure(bcs.vector(bcs.vector(bcs.u8())).serialize(config.keys.map(k => [...fromBase64(k)]))), tx.pure.vector("u8", config.weights), tx.pure.u16(config.threshold), tx.object("0x6")] });
  return tx;
}
export interface MultisigEnvelope { schema: 1; chain: string; packageId: string; customerId: string; config: MultisigConfiguration; bytes: string; signatures: string[] }
export function readEnvelope(json: string): MultisigEnvelope {
  if (json.length > 1_000_000) throw Error("Transaction bundle is too large");
  const e = JSON.parse(json) as MultisigEnvelope, manifest = requireManifest();
  if (e.schema !== 1 || e.chain !== manifest.chain_identifier || e.packageId !== PACKAGE_ID || !Array.isArray(e.signatures) || e.signatures.length > 10) throw Error("Unsupported bundle or deployment mismatch");
  normalizeSuiAddress(e.customerId);
  const tx = Transaction.from(fromBase64(e.bytes)).getData(), address = multisigKey(e.config).toSuiAddress();
  if (tx.sender !== address || tx.gasData.owner !== address) throw Error("Bundle sender and gas owner must be the configured multisig");
  return e;
}
export async function exportMultisig(client: SuiGrpcClient, customerId: string, config: MultisigConfiguration, tx: Transaction) {
  await verifyDeployment(client);
  const address = multisigKey(config).toSuiAddress();
  tx.setSender(address); tx.setGasOwner(address);
  const bytes = await tx.build({ client });
  return readEnvelope(JSON.stringify({ schema: 1, chain: requireManifest().chain_identifier, packageId: PACKAGE_ID, customerId, config, bytes: toBase64(bytes), signatures: [] }));
}
export async function addPartial(envelope: MultisigEnvelope, signature: string) {
  const e = readEnvelope(JSON.stringify(envelope)), key = multisigKey(e.config), bytes = fromBase64(e.bytes);
  const signatures = [...e.signatures, signature], seen = new Set<string>();
  for (const sig of signatures) {
    const signer = await verifyTransactionSignature(bytes, sig);
    if (!key.getPublicKeys().some(k => k.publicKey.flag() === signer.flag() && k.publicKey.equals(signer))) throw Error("Signature is not from a configured member");
    const address = signer.toSuiAddress();
    if (seen.has(address)) throw Error("Duplicate member signature");
    seen.add(address);
  }
  return { ...e, signatures };
}
export async function submitMultisig(client: SuiGrpcClient, envelope: MultisigEnvelope) {
  const e = readEnvelope(JSON.stringify(envelope)), key = multisigKey(e.config);
  let checked = { ...e, signatures: [] as string[] };
  for (const signature of e.signatures) checked = await addPartial(checked, signature);
  const signature = key.combinePartialSignatures(checked.signatures), transaction = fromBase64(e.bytes);
  if (!await key.verifyTransaction(transaction, signature)) throw Error("Multisig threshold is not satisfied");
  await verifyDeployment(client);
  const digest = await Transaction.from(transaction).getDigest();
  const result = await client.executeTransaction({ transaction, signatures: [signature], include: { effects: true } }).catch(() => {
    throw Error(`Submission outcome unknown for ${digest}; reconcile this digest before retrying`);
  });
  if (result.FailedTransaction || !result.Transaction.status.success) throw Error(`Transaction ${digest} failed: ${result.FailedTransaction?.status.error?.message ?? "unsuccessful effects"}`);
  const confirmed = await client.waitForTransaction({ digest: result.Transaction.digest, include: { effects: true } }).catch(() => { throw Error(`Confirmation unavailable for ${digest}; reconcile before retrying`); });
  if (confirmed.FailedTransaction || !confirmed.Transaction.status.success) throw Error(`Transaction ${digest} confirmation failed`);
  return confirmed.Transaction.digest;
}

/** Build the canonical policy intent on chain; customer BCS binds its full state. */
export async function multisigPolicyTx(client: SuiGrpcClient, customerId: string, config: MultisigConfiguration, requirements: number[], actionId?: string) {
  await verifyDeployment(client);
  if (requirements.length !== 7 || requirements.some(v => !Number.isInteger(v) || v < 0 || v > 15)) throw Error("Expected seven policy masks");
  const tx = new Transaction();
  if (!actionId) {
    tx.moveCall({ target: `${PACKAGE_ID}::multisig_admin::prepare_policy_change`, arguments: [tx.object(customerId), tx.pure.vector("u8", requirements), tx.pure.u64(Date.now() + 3600000), tx.object("0x6")] });
  } else {
    // Rebuild the same call so approval and consumption are one atomic PTB.
    tx.moveCall({ target: `${PACKAGE_ID}::multisig_admin::approve`, arguments: [tx.object(customerId), tx.object(actionId), tx.pure(bcs.vector(bcs.vector(bcs.u8())).serialize(config.keys.map(k => [...fromBase64(k)]))), tx.pure.vector("u8", config.weights), tx.pure.u16(config.threshold), tx.object("0x6")] });
    tx.moveCall({ target: `${PACKAGE_ID}::customer::set_security_secured`, arguments: [tx.object(customerId), tx.pure.vector("u8", requirements), tx.object(actionId), tx.object("0x6")] });
  }
  return tx;
}
