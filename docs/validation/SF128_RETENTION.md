# SF-128 scalable protected retention

Codex gpt-6.1-sol (no finer build identifier exposed), session
dispatch-d0b5a9003cfe451e8a0e5038acb9bcc7; assigned branch
work/suifleet/sf-128-159af039. [Issue #258](https://github.com/tyler-suifleet/suifleet/issues/258).
The unchanged device admission API and new fully-paid behavior were coordinated
with SF-121 in [its existing issue](https://github.com/tyler-suifleet/suifleet/issues/252#issuecomment-6374524198).

V3 uses existing per-blob RenewalKey dynamic fields directly: no bounded vector
or fleet-wide expiry scan. Explicit owner migration removes only the legacy
vector and updates the version, preserving layouts, origins, records, prepaid
resources and counters. Older mutation calls reject migrated objects. Removal
and renewal policy replacement touch only the selected blob.

Fully purchased device archives use a separate FullyPaidArchiveKey marker with
no renewal/prepayment/reward obligation. Absolute admission targets must remain
in the future. Owner recipe/release registration still requires renewal.
An expired blob needs its own restoration/reference review; unrelated writes
continue when coverage, reserves, pool and quotas permit. No automatic deletion,
restoration or reboot is added.

## Verification

Pinned container suifleet-dev:sf035: Sui 1.81.1-e04cf9aa915c, Node 22.22.2.
The checkout was mounted at /workspace and the pinned Move cache at
/home/dev/.move. Historical Git metadata was mounted read-only at its original
absolute path. Disposable chains used network none and no host ports.

| Command | Result |
|---|---|
| sui move test --path contracts/fleet_storage | 61 passed |
| make test-retention | 14 Python tests and SDK/BCS/browser regressions passed |
| node scripts/test_fleet_budget.cjs | Passed |
| python3 scripts/generate_interfaces.py --check | Passed: 11 modules and generated interfaces |
| cd dapp && pnpm typecheck && pnpm build | Passed, static export under pinned Node 22 |
| python3 scripts/test_protocol_localnet.py --retention-scale-upgrade | Scale phase passed; later harness input error corrected below |
| python3 scripts/test_protocol_localnet.py --retention-keyed-upgrade | All populated upgrade, expiry and device checks passed, exit 0 |

Browser coverage includes 100 pages / 10,000 records. The worker test covers
10,000 completed records, one expired record, successful unrelated work and restart.
Local scale uses real upstream Walrus layouts, wrapped 10-byte RS2 blob registration
and actual renewal storage resources, with deterministic single-member committee,
price and epoch-control fixture instrumentation. No public storage-node
certification or customer upload is claimed. Populated v1/v2 upgrades, prepaid
preservation, old-entry rejection, per-blob expiry, unrelated admission and actual
enrolled-device finite-target/fully-paid checks are in the localnet receipt.

Local browser/executor safety bounds remain 100,000 records, 128 MiB journal and
64 MiB transport response. These do not constrain chain admission. Above them,
separate storage objects or a future paged executor are needed before relying on
unattended renewal; no unimplemented paging is claimed.

## Public Testnet

[sf128-testnet.json](sf128-testnet.json) records source/module hashes, package IDs,
origins, exact digests, pinned CLI provenance and effects-derived costs.
Final working-tree compiler bytecode was independently compared to the upgraded
package by read-only chain queries.

Chain 4c78adac, protocol 139; dedicated agent
0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965.
A private runtime config adapted only the keystore path from the supplied dedicated
wallet config. Keys remained private. The shared mutation lock was asserted,
renewed and released; coordination was posted in #11/#63. No faucet, customer
object, other wallet or physical/mainnet operation was used.

The first completed revision run spent 257,837,902 MIST, allowance used
258,037,902 / 500,000,000 MIST. After the finite-target guard, the separate final
run spent 258,080,108 MIST, allowance used 258,268,302 / 350,000,000 MIST.
Both non-replenishing ledgers are preserved: total actual spend 515,918,010 MIST,
not a single 0.5 SUI run. Identical core bytecode was reused and reverified.
Each revised (base, head) pair required its own base fixture because upgrades
chain only from the latest version.

The final seven transactions prove compatible v2-to-v3 upgrade, existing
retention-configured object migration, original/later type origins and expected
old-v2 on-chain mutation rejection. Its legacy vector was empty; populated
real-blob/prepaid migration and 10,000-record gas are isolated-chain evidence.

Reproduce with scripts/live_storage_upgrade.py, an explicit wallet/config,
held machine mutation lock, pinned CLIs, chain-facts reader, private fixture cache
and declared spending ceiling. Bytecode, UpgradeCap ownership/version and effects
are checked. Preserve those safeguards and cached fixture reuse.

## Remaining acceptance

Exact-head CI, automatic current-head/base Testnet status and fresh independent
review remain required. The PR coordinator owns review and integration; no merge,
installation or deployment was performed by this worker.

Physical/target, mainnet, production-provider/account and manual-wallet acceptance
remain unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
This does not establish public certification of the local scale fixture, customer
recovery, hardware behavior, production availability or full Greengrass parity.

## Registration gas

The final-source scale phase completed 10,001 real retained registrations.
Effects gas for a single registration was identical with 10, 100, 1,000 and
10,000 prior protected records:

| Component | MIST |
|---|---:|
| Computation | 1,670,000 |
| Storage cost | 46,747,600 |
| Storage rebate | 31,360,032 |
| Net | 17,057,568 |

The combined scale invocation later stopped on an expected-failure transaction
whose shared Clock was incorrectly marked mutable by the test harness. That
construction was fixed; the relevant populated-upgrade/expiry/device suite was
rerun successfully with the keyed-upgrade command. The unchanged completed
10,000-record scale phase is retained separately, not mislabeled as an entirely
successful combined invocation. Exact local receipts are in
[sf128-localnet.json](sf128-localnet.json). These fixture prices/gas are not
production pricing, certification or representative archive-size economics.
