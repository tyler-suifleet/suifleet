import { bcs, TypeTagSerializer } from '@mysten/sui/bcs';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { ObjectError } from '@mysten/sui/client';
import { deriveDynamicFieldID, normalizeStructTag, normalizeSuiAddress } from '@mysten/sui/utils';

/** Read ordinary dynamic fields by deterministic ID in bounded object batches.
 * Validate the complete Field type, UID and serialized key before decoding values.
 */
export async function batchFields(client: Pick<SuiGrpcClient, 'getObjects'>, requests: { parent: string; keyType: string; key: Uint8Array; valueType: string }[]) {
  const ids = requests.map(r => deriveDynamicFieldID(r.parent, TypeTagSerializer.parseFromStr(r.keyType), r.key));
  const values: (Uint8Array | undefined)[] = [];
  for (let offset = 0; offset < ids.length; offset += 50) {
    const { objects } = await client.getObjects({ objectIds: ids.slice(offset, offset + 50), include: { content: true } });
    if (objects.length !== Math.min(50, ids.length - offset)) throw Error('Incomplete dynamic field batch');
    objects.forEach((object, i) => {
      if (object instanceof ObjectError && object.reason === 'notFound') { values.push(undefined); return; }
      if (object instanceof Error) throw object;
      const index = offset + i, request = requests[index];
      const expectedType = `0x2::dynamic_field::Field<${request.keyType},${request.valueType}>`;
      if (normalizeStructTag(object.type) !== normalizeStructTag(expectedType) || normalizeSuiAddress(object.objectId) !== ids[index]
        || object.content.length < 32 + request.key.length || bcs.Address.parse(object.content.slice(0, 32)) !== ids[index]
        || request.key.some((byte, n) => byte !== object.content[32 + n])) throw Error('Dynamic field type, ID or key mismatch');
      values.push(object.content.slice(32 + request.key.length));
    });
  }
  return values;
}
