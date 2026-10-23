// Bound storage-node traffic on devices. The SDK still verifies quorum and
// signatures in certificateFromConfirmations; failed nodes contribute no vote.
export async function writeBounded(storage, encoded, objectId, concurrency = 8) {
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 16) throw Error('Invalid node concurrency');
  const results = Array(encoded.sliversByNode.length).fill(null);
  for (let attempt = 0; attempt < 2; attempt++) {
  let next = 0;
  await Promise.all(Array.from({length: Math.min(concurrency, results.length)}, async () => {
    while (next < results.length) {
      const nodeIndex = next++;
      if (results[nodeIndex]) continue;
      try {
        results[nodeIndex] = await storage.writeEncodedBlobToNode({nodeIndex, blobId: encoded.blobId,
          metadata: encoded.metadata, slivers: encoded.sliversByNode[nodeIndex], deletable: false,
          objectId, signal: AbortSignal.timeout(10_000)});
      } catch { /* No confirmation: the SDK certificate quorum check decides success. */ }
    }
  }));
  }
  return results;
}

// The pinned SDK verifies signatures but counts nodes. The Move contract
// requires a quorum of shard weight; never persist/sign a weaker certificate.
export function requireWeightedQuorum(committee, signers) {
  if (!committee || !Array.isArray(committee.members) || !Number.isSafeInteger(committee.n_shards) || committee.n_shards <= 0) throw Error('Invalid Walrus committee');
  const weights = committee.members.map(m => m.weight);
  if (!weights.every(w => Number.isSafeInteger(w) && w >= 0) || weights.reduce((a,b) => a+b,0) !== committee.n_shards) throw Error('Invalid Walrus committee weights');
  if (!Array.isArray(signers) || new Set(signers).size !== signers.length || !signers.every(i => Number.isInteger(i) && i >= 0 && i < weights.length)) throw Error('Invalid Walrus certificate signers');
  const weight = signers.reduce((sum,i) => sum + weights[i],0);
  if (3 * weight < 2 * committee.n_shards + 1) throw Error('Insufficient Walrus shard-weight quorum');
  return weight;
}
