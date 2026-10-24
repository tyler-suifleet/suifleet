// One shared request budget per browser tab, including proof/receipt reads and
// SDK-internal calls. No initial burst: at most 86 starts in any 30 seconds.
export const RPC_SPACING_MS = 350;
export function budgetedFetch(fetcher: typeof fetch = (...args) => fetch(...args), now = () => Date.now(),
  delay: (ms: number) => Promise<void> = ms => new Promise(resolve => setTimeout(resolve, ms))): typeof fetch {
  let tail = Promise.resolve(), next = 0, queued = 0;
  return async (input, init) => {
    if (queued >= 500) throw Error('Fleet request queue full; wait for current reads to finish');
    queued++;
    const turn = tail.then(async () => {
      init?.signal?.throwIfAborted();
      const wait = next - now();
      if (wait > 0) await delay(wait);
      init?.signal?.throwIfAborted();
      next = now() + RPC_SPACING_MS;
    });
    tail = turn.catch(() => {});
    try { await turn; return await fetcher(input, init); }
    finally { queued--; }
  };
}
export const fleetFetch = budgetedFetch();
