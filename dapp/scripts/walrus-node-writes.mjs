// Bound storage-node traffic on devices. The SDK still verifies quorum and
// signatures in certificateFromConfirmations; failed nodes contribute no vote.
export async function writeBounded(storage, encoded, objectId, concurrency = 8) {
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 16) throw Error('Invalid node concurrency');
  const results = Array(encoded.sliversByNode.length).fill(null);
  let next = 0;
  await Promise.all(Array.from({length: Math.min(concurrency, results.length)}, async () => {
    while (next < results.length) {
      const nodeIndex = next++;
      try {
        results[nodeIndex] = await storage.writeEncodedBlobToNode({nodeIndex, blobId: encoded.blobId,
          metadata: encoded.metadata, slivers: encoded.sliversByNode[nodeIndex], deletable: false,
          objectId, signal: AbortSignal.timeout(10_000)});
      } catch { /* No confirmation: the SDK certificate quorum check decides success. */ }
    }
  }));
  return results;
}
