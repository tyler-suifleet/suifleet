# SF-090 candidate pilot experiment

This workload was declared before measurement. It is a candidate for validation,
not an agreed customer SLA or a supported fleet capacity. No production pilot is
approved by a successful fixture benchmark. Production provider quotas, accounts,
pricing and availability remain unquoted, with final acceptance in
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Physical/manual wallet acceptance remains in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).

The versioned workload is `workload.json`: 128 distinct synthetic signing identities,
four concurrent consumers, three rounds, and 16 KiB / 256 KiB / 1 MiB log payloads.
Signed JSON/base64 and Seal overhead are additional to payload bytes. Reports are
hourly, logs daily, retention three purchased epochs. These cadences describe the
candidate operating profile; accelerated fixture rounds do not simulate elapsed
retention or a multi-day soak. Browser enumeration must cross the real reader's
100-device page boundary. A 1 GiB process RSS ceiling and explicit p95 limits are
regression guards on the measured machine, not provider latency promises.

The harness uses actual Ed25519 personal-message signatures, pinned SDK Seal
threshold encryption, independent AES-GCM opening and the shipped verified
health/archive/index readers. Key discovery and chain/storage replies are fixtures;
the DEM key is test-only and never evidence of real keyserver authorization.
Retry/concurrency controls belong to the experiment driver; measuring them does
not prove the daemon/browser implements the same scheduler. No fixture budget
counter is presented as chain-enforced spending. Network spend is zero only for
the fixture run. Actual Sui/WAL costs require effects/quotes separately, and cannot
be extrapolated from CPU timings.

Required final integrated evidence still includes enrollment/deployment/receipt
latency, real budget/storage contention, isolated multi-device end-to-end soak,
bounded public-testnet samples and real browser responsiveness. Historical producer
receipts are references, never relabeled as a current SF-090 load run. Until these
are recorded, supported paid-pilot limits are **not established**. Unsupported:
more than the measured workload, production availability/SLA, long-term retention,
firmware safety, hardware resource use, and production provider quotas.
