# Public Walrus artifact delivery (SF-124)

Devices need no SuiFleet artifact server. Configure an ordered set of public
aggregators in the deployment manifest; operators maintain the list rather than
having devices trust an automatically downloaded operator catalog.

```json
"walrus": {
  "publisher_url": "https://publisher.example",
  "aggregator_url": "https://aggregator.walrus-mainnet.walrus.space",
  "aggregator_urls": [
    "https://aggregator.walrus-mainnet.walrus.space",
    "https://aggregator.mainnet.walrus.mirai.cloud"
  ]
}
```

These mainnet URLs are read examples, not a deployed SuiFleet mainnet release.
Use endpoints for the blob's Walrus network. `aggregator_urls`, when nonempty,
replaces the single device endpoint. Missing/empty lists use `aggregator_url`,
which remains required for older clients. The browser tries the ordered list
then the legacy `aggregator_url`; browser endpoints need HTTPS and CORS access. Schema version stays
1; old clients ignore the optional list. The daemon validates every URL and
rejects a nonempty device-config list that differs from the manifest.

The manifest resolver and `scripts/redeploy.sh` accept repeated
`--aggregator-mirror URL` options in priority order. Publishing/upgrading preserves
an existing list unless replacements are supplied; pending publication records
also retain it for recovery. Resolve/generate alone are read-only; publishing is
still governed by the normal network authorization and deployment preflight.

Optional device settings:

```toml
[walrus]
range_chunk_bytes = 4194304       # default 4 MiB; valid 1..=10485760
download_attempts = 4
download_timeout_seconds = 3600 # entire blob, all mirrors/attempts
```

Artifact requests use inclusive `Range: bytes=start-end` headers from the first
request onward. Each range is bounded by `range_chunk_bytes`; the final request
may extend beyond EOF and the response must describe its actual bounded range.
The daemon checks the complete numeric `Content-Range`, response length, total
artifact quota and free-space reserve. Unknown-length bodies also obey the
range and disk limits. A server ignoring Range may supply a complete blob only
if it fits within the chunk limit. Large whole-blob responses are rejected.

Transient drops, timeouts, 429 and 5xx retry on a mirror, resuming the persisted
`.partial` file. Errors (including 404/410, invalid ranges, exhausted retries,
truncation and incorrect artifact bytes) advance to the next mirror. Fallback
starts from zero so a bad mirror cannot poison the next mirror's download.
The first attempt after a daemon restart can resume persisted bytes; all bytes
still require the final digest. Mirrors each receive an equal share of the
overall timeout so a stalled first endpoint cannot consume every mirror's time.
A single-mirror deployment keeps its full timeout. Recipes, limited to 1 MiB,
also fall back on transport, digest and decryption errors before parsing or
caching. Their digest/decryption checks keep the same authority and algorithms.

The SHA-256 authority remains the signed recipe. Artifact integrity checks run
inside the mirror loop before the updater or artifact cache sees success.
Seal ciphertext must decrypt successfully and match the signed plaintext digest;
required-confidential mode still rejects plaintext. A corrupt completed candidate
is deleted before fallback. Transport completion or a Walrus blob ID alone does
not establish trusted artifact contents.

## Read-only public measurements, 2026-10-23

[Raw results](validation/sf124-public-aggregators.json) record all 24 curl reads,
status, headers, byte counts, timing and SHA-256. No upload, wallet transaction,
credentials or mainnet write was used. Bodies were discarded after hashing.
Two public blobs were selected from mainnet Walrus `BlobRegistered` events using
the read-only Sui GraphQL endpoint; their sizes are 177,277,057 and 168,732,005
bytes, both above 10 MiB. The evidence contains the selected event metadata.

The [network reference](https://docs.wal.app/docs/network-reference) describes a
10 MiB public request limit without establishing a read-response cap. The
[media API](https://docs.wal.app/docs/http-api/streaming-media) documents byte
ranges. Measurements tested full GET, a 4 MiB Range twice, then a 12 MiB Range
against each blob on three listed public mainnet aggregators. They do **not**
establish a universal service limit or availability guarantee.

| Public mainnet aggregator | Full reads | 4 MiB Range reads (two blobs, two requests each) | Cache observations |
|---|---|---|---|
| Mysten (`aggregator.walrus-mainnet.walrus.space`) | `200`, 177.3/168.7 MB; 16.65/20.10 s | `206`, exactly 4 MiB; 7.31–8.69 MiB/s | Cloudflare `HIT`, `Age`; origin `x-cache-status: MISS` can coexist with CDN hit |
| H2O (`aggregator.walrus-mainnet.h2o-nodes.com`) | `200`, 177.3/168.7 MB; 17.76/17.38 s | Range ignored: `200`, whole blob; 7.33–9.43 MiB/s for the **whole response**, not ranged throughput | `x-cache-status: HIT`; public max-age 86400 with stale-while-revalidate 3600 |
| Mirai (`aggregator.mainnet.walrus.mirai.cloud`) | `200`, 177.3/168.7 MB; 30.56/22.05 s | `206`, exactly 4 MiB; 5.76–7.32 MiB/s | Cloudflare `HIT`, `Age`; public max-age 86400 with stale-while-revalidate 3600 |

All three returned more than 10 MiB on full reads. Mysten and Mirai also returned
12 MiB `206` responses; H2O again ignored that Range and returned the entire blob.
Thus a 10 MiB **read** cap was not observed on these endpoints/blobs. This says
nothing about upload limits or other endpoints. Keep bounded 4 MiB requests for
compatible public services, and avoid H2O as the sole mirror for large artifacts.
The 4 MiB response hashes agreed between Mysten and Mirai; complete-blob hashes
agreed across all three. Those comparison hashes are measurement evidence, not
signed SuiFleet recipe authority.

Requests were sequential from this WSL host; timing includes connection and
transfer. Cache headers describe the observed CDN/origin layers only. Exploratory
reads had already warmed caches, and no purge or cold-cache experiment was
performed. A repeated hit or speed difference does not prove an operator's cache
architecture or sustained performance. Initial Python urllib requests received
403s from some CDN paths; the recorded comparison uses curl consistently.

To reproduce a small ranged read without writes (blobs may eventually expire):

```bash
curl --max-time 45 -D headers.txt -r 0-4194303 \
  -o range.bin -w 'HTTP %{http_code}; %{size_download} bytes; %{speed_download} B/s\n' \
  'https://aggregator.walrus-mainnet.walrus.space/v1/blobs/hHik75L7cwTdr4dXaOK_4xPrCuIrqUg2fqpS62YDO7Y'
sha256sum range.bin
```

For the measurement matrix, substitute each recorded endpoint and blob; omit
`-r` for a full read or use `-r 0-12582911` for 12 MiB. Full reads can transfer
hundreds of MB, especially when a server ignores Range. The experiment used a
45-second deadline and a 256 MiB response ceiling, and saved only metadata and
hashes. Physical/real-wallet/production-provider/mainnet-write acceptance remains
unrun under [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
