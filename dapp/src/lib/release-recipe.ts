// Explicit, memory-only inspection of confidential recipe references for renewal.
import { EncryptedObject, SealClient, type SessionKey } from '@mysten/seal';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction } from '@mysten/sui/transactions';
import { fromHex, toHex } from '@mysten/sui/utils';
import { MAGIC, isEnvelope, aad, type SealSettings } from './seal';
import { sealNamespace, policyPackage } from './access-policy';
import { normalizeId } from './deployment-manifest';
import { assertComponentRecipe } from './recipe-schema';

export async function verifiedRecipeArtifact(plaintext: Uint8Array, sha256: string) {
  try {
    if (!/^[0-9a-f]{64}$/i.test(sha256) || plaintext.length > 1_048_576) throw Error('Recipe digest or size is invalid');
    const digest = toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', Uint8Array.from(plaintext))));
    if (digest !== sha256.toLowerCase()) throw Error('Recipe digest mismatch');
    const recipe: unknown = JSON.parse(new TextDecoder().decode(plaintext));
    assertComponentRecipe(recipe);
    return recipe.artifactBlobId;
  } finally { plaintext.fill(0); }
}
export async function inspectConfidentialRecipe(client: SuiGrpcClient, bytes: Uint8Array, sha256: string,
  settings: SealSettings, session: SessionKey) {
  if (!isEnvelope(bytes) || bytes.length > 1_048_576 || !/^[0-9a-f]{64}$/i.test(sha256)) throw Error('Invalid confidential recipe');
  const namespace = sealNamespace();
  if (session.isExpired() || normalizeId(session.getPackageId()) !== normalizeId(namespace)) throw Error('Expired or foreign recipe session');
  const encrypted = bytes.slice(MAGIC.length), object = EncryptedObject.parse(encrypted), identity = fromHex(object.id);
  const equal = (a: ArrayLike<number>, b: ArrayLike<number>) => a.length === b.length && Array.from(a).every((v, i) => v === b[i]);
  const servers = settings.keyServers.flatMap(s => Array(s.weight).fill(normalizeId(s.objectId))).sort();
  if (normalizeId(object.packageId) !== normalizeId(namespace) || identity.length < 56 || identity.length > 104
    || object.threshold !== settings.threshold || object.services.map(([id]) => normalizeId(id)).sort().join() !== servers.join()
    || !object.ciphertext.Aes256Gcm?.aad || !equal(object.ciphertext.Aes256Gcm.aad, aad('recipe', sha256))) throw Error('Confidential recipe binding mismatch');
  const tx = new Transaction(); tx.setSender(session.getAddress());
  tx.moveCall({ target: `${policyPackage()}::seal_policy::seal_approve_account`, arguments: [tx.pure.vector('u8', identity), tx.object(`0x${toHex(identity.slice(0, 32))}`)] });
  const seal = new SealClient({ suiClient: client, serverConfigs: settings.keyServers, verifyKeyServers: true });
  const plaintext = await seal.decrypt({ data: encrypted, sessionKey: session, txBytes: await tx.build({ client, onlyTransactionKind: true }), checkShareConsistency: true });
  if (session.isExpired()) { plaintext.fill(0); throw Error('Recipe session expired'); }
  return verifiedRecipeArtifact(plaintext, sha256);
}

export async function boundedRecipeBytes(response: Response): Promise<Uint8Array> {
  if (!response.ok || !response.body) throw Error('Recipe download unavailable');
  const reader = response.body.getReader(), chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 1_048_576) throw Error('Recipe exceeds inspection size limit');
      chunks.push(value);
    }
    const bytes = new Uint8Array(size); let at = 0;
    for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.length; }
    return bytes;
  } finally { await reader.cancel(); reader.releaseLock(); }
}
export async function deploymentRecipeDigest(client: SuiGrpcClient, deploymentId: string) {
  const { bcs } = await import('@mysten/sui/bcs');
  const { ObjectError } = await import('@mysten/sui/client');
  const { normalizeStructTag } = await import('@mysten/sui/utils');
  const { moveType, verifyDeployment } = await import('./deployment-manifest');
  await verifyDeployment(client);
  try {
    const { dynamicField } = await client.getDynamicField({ parentId: deploymentId, name: { type: moveType('deployment_manager::RecipeDigestKey'), bcs: new Uint8Array([0]) } });
    if (normalizeStructTag(dynamicField.value.type) !== normalizeStructTag('0x1::string::String')) throw Error('Invalid deployment recipe digest type');
    const digest = bcs.string().parse(dynamicField.value.bcs);
    if (!/^[0-9a-f]{64}$/i.test(digest)) throw Error('Invalid deployment recipe digest');
    return digest;
  } catch (e) { if (e instanceof ObjectError && e.reason === 'notFound') return undefined; throw e; }
}
