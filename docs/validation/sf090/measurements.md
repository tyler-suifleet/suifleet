# Merged-source measurements

These are measured software boundaries, with public-provider and native-fleet limits in [README](README.md). Nearest-rank times are milliseconds.

| Experiment / stage | n | p50 | p95 |
|---|---:|---:|---:|
| chain: enrollment | 16 | 747.36 | 1674.44 |
| chain: deployment_admission | 1 | 4884.79 | 4884.79 |
| chain: budget_draw | 16 | 774.38 | 1670.41 |
| chain: health_anchor | 16 | 835.88 | 1719.20 |
| chain: telemetry_visibility | 16 | 13.26 | 33.91 |
| chain: encrypted_fetch | 16 | 692.51 | 1786.20 |
| chain: synthetic_apply | 16 | 3.99 | 9.51 |
| chain: receipt_finalization | 16 | 761.12 | 1687.20 |
| chain: index_page | 1 | 6.78 | 6.78 |
| chain: storage_contention | 4 | 1663.16 | 1664.88 |
| fixtures: index_page | 2 | 0.61 | 3.47 |
| fixtures: health_page | 2 | 41.24 | 159.27 |
| fixtures: sign_encrypt | 384 | 269.18 | 755.86 |
| fixtures: verified_fetch | 384 | 81.79 | 575.55 |
| browser: initial | 5 | 67.32 | 183.74 |
| browser: page_recovery | 5 | 51.54 | 62.95 |
| Testnet effects reads | 8 | 82.16 | 306.17 |

| Receipt | Executed revision |
|---|---|
| [merged-chain](merged-chain.json) | `ce6096b2e129d97b877b56f07b6f13c4b76c4a80` |
| [merged-fixtures](merged-fixtures.json) | `a30a00606b317329c2134c39881eb0bcdc653e28` |
| [merged-browser](merged-browser.json) | `a30a00606b317329c2134c39881eb0bcdc653e28` |
| [merged-daemon](merged-daemon.json) | `ffb1c630e5f69f4003855d79729f6e75f3747cbc` |
| [merged-public-read](merged-public-read.json) | `a30a00606b317329c2134c39881eb0bcdc653e28` |

Integrated-chain Node peak RSS: 331,874,304 bytes (excludes child node/compiler/keyservers). Crypto-fixture Node peak RSS: 454,287,360 bytes. Chromium renderer JS-heap peak: 23,044,180 bytes (not browser RSS).

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
