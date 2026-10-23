# Merged-source measurements

These are measured software boundaries, with public-provider and native-fleet limits in [README](README.md). Nearest-rank times are milliseconds.

| Experiment / stage | n | p50 | p95 |
|---|---:|---:|---:|
| chain: enrollment | 16 | 768.40 | 1715.09 |
| chain: deployment_admission | 1 | 3693.77 | 3693.77 |
| chain: budget_draw | 16 | 1640.67 | 1717.56 |
| chain: health_anchor | 16 | 797.99 | 1649.25 |
| chain: telemetry_visibility | 16 | 19.10 | 248.10 |
| chain: encrypted_fetch | 16 | 790.16 | 1722.52 |
| chain: synthetic_apply | 16 | 11.82 | 293.13 |
| chain: receipt_finalization | 16 | 876.75 | 1699.87 |
| chain: index_page | 1 | 7.18 | 7.18 |
| chain: storage_contention | 4 | 639.78 | 641.38 |
| fixtures: index_page | 2 | 0.61 | 3.46 |
| fixtures: health_page | 2 | 40.74 | 155.52 |
| fixtures: sign_encrypt | 384 | 290.83 | 791.00 |
| fixtures: verified_fetch | 384 | 84.45 | 603.87 |
| browser: initial | 5 | 72.39 | 245.36 |
| browser: page_recovery | 5 | 51.44 | 57.21 |
| Testnet effects reads | 8 | 81.56 | 115.34 |

| Receipt | Executed revision |
|---|---|
| [merged-chain](merged-chain.json) | `a187418d3dff48bf0cc73fcea2854db7576d7950` |
| [merged-fixtures](merged-fixtures.json) | `ffb1c630e5f69f4003855d79729f6e75f3747cbc` |
| [merged-browser](merged-browser.json) | `ffb1c630e5f69f4003855d79729f6e75f3747cbc` |
| [merged-daemon](merged-daemon.json) | `ffb1c630e5f69f4003855d79729f6e75f3747cbc` |
| [merged-public-read](merged-public-read.json) | `ffb1c630e5f69f4003855d79729f6e75f3747cbc` |

Integrated-chain Node peak RSS: 334,274,560 bytes (excludes child node/compiler/keyservers). Crypto-fixture Node peak RSS: 446,615,552 bytes. Chromium renderer JS-heap peak: 23,176,384 bytes (not browser RSS).

Four independent native restart drills: 111.31 seconds at concurrency two; not one shared-chain fleet.

Completed integrated isolated run: 97 successful transactions, 4 quota-refused draws and 4 storage contenders. Net gas 1922450910 MIST, including successful and failed effects and package setup; synthetic local rates, not public cost. Public sample: zero writes/new spend. Earlier failed disposable attempts are not included in the completed-run total.

All completed receipt thresholds pass; faults/expected refusals are separate from unexpected failures. Production quotas, invoices, provider availability and paid capacity remain unestablished.

---

# SF-090 measured facets

These are separate experiments against the unmerged producer pin. They are not
a whole-product paid-pilot throughput, capacity or availability result. See
[scope, reproduction and blockers](README.md). Times are milliseconds.

| Boundary / stage | n | p50 | p95 |
|---|---:|---:|---:|
| crypto/consumer fixture: index_page | 2 | 0.56 | 3.27 |
| crypto/consumer fixture: health_page | 2 | 41.72 | 150.59 |
| crypto/consumer fixture: sign_encrypt | 384 | 302.37 | 810.65 |
| crypto/consumer fixture: verified_fetch | 384 | 84.24 | 617.66 |
| isolated Sui: enrollment | 16 | 778.98 | 1680.77 |
| isolated Sui: deployment_admission | 1 | 440.87 | 440.87 |
| isolated Sui: budget_draw | 16 | 769.13 | 1740.96 |
| isolated Sui: health_anchor | 16 | 1655.08 | 1712.45 |
| isolated Sui: telemetry_visibility | 16 | 24.10 | 32.14 |
| isolated Sui: receipt_finalization | 16 | 744.09 | 1639.44 |
| isolated Sui: index_page | 1 | 3.47 | 3.47 |
| isolated Sui: storage_contention | 4 | 1657.96 | 1659.36 |
| Chromium fixture transport: initial | 5 | 65.87 | 213.24 |
| Chromium fixture transport: page_recovery | 5 | 53.74 | 56.60 |
| public historical effects read: read | 8 | 77.84 | 124.57 |

| Log payload bytes | sign/encrypt p95 | verified fetch p95 |
|---:|---:|---:|
| 16384 | 226.38 | 20.63 |
| 262144 | 372.17 | 169.94 |
| 1048576 | 816.98 | 626.84 |

Fixture process peak RSS: 460,369,920 bytes; CPU user/system
59,045,191/5,921,673 microseconds.
Isolated experiment Node process peak RSS: 200,179,712 bytes; excludes
the node/compiler child processes and host caches.
Chromium renderer JS heap peak over five page reloads: 23,116,452
bytes; this is not full browser RSS or a steady-state leak test.

Four native restart drills: 111.32 seconds elapsed at concurrency two.
Each drill is an independent simulator with its test identity, not one shared fleet.

Isolated chain: 91 successful transactions, four quota-refused draws,
one successful and three insufficient-storage concurrent registrations.
Summed experiment gas: 1888231210 MIST, including successful and failed effects
and package setup; synthetic faucet/local rates, not a public cost quote. Failed
earlier disposable setup attempts are not included in this completed-run sum.

Public sample: 8 fresh independent effects readbacks; zero writes and
zero newly spent SUI/WAL. The historical gas recorded per digest belongs to the
producer experiment and is not reclassified as current experiment spending.
No public enrollment/upload/keyserver capacity or latency was measured.

Threshold failures in completed experiment receipts: zero. Injected faults and
expected on-chain refusals are separately retained. Initial setup failures and
the repaired tracking regression are described in README.md; they are not
erased by these passing measurements.

External provider quotas, paid capacity, actual service bill, production fees,
retention availability and supported paid-pilot limits remain **unestablished**.
Existing SF-054/SF-082 quotes may guide later budgeting; a quote is not a bill.

| Receipt | Actual recorded source revision |
|---|---|
| [fixture](fixture-run.json) | `fc972b68b28f0248c3522f0fae365065c8b0848d` |
| [isolated](isolated-run.json) | `5e8f1195b2f8e1aabb01e512c11819f2f6408a6f` |
| [browser](browser-run.json) | `17de3973f63f205c2506d53c915256ec38921835` |
| [daemon](daemon-run.json) | `815f2b31c19e6ef35027b1bb3449bc54f6147cd7` |
| [public-read](public-read-run.json) | `815f2b31c19e6ef35027b1bb3449bc54f6147cd7` |

