# SF-196 framework recovery evidence

Codex, actual configured model gpt-6.1-sol (no finer runtime build exposed).
Same retained session dispatch-487779ccfe9444d1b0ba30c4fbf895d0 and branch
work/suifleet/sf-196-267c81b9, [PR421](https://github.com/tyler-suifleet/suifleet/pull/421).
User design approval is applied; no human/Pi/mainnet implementation hold remains.
Initial working tree was clean; no dirty/staged work was discarded. No new
issues, workers, merge or application deployment was performed.

The implemented framework closes custody/external producer scope, catalog,
budget/storage withdrawals, handover, domain/revision/class/pause and enrollment
boundaries. Authority197 retires both legacy1 and the published intermediate196.
All four mechanism availability bits remain zero: SF-198/199/200/201 own their
actual verifiers, enrollment and activation UI. The page shows unavailable
mechanisms honestly. Generic external data/storage administration has no raw-cap
escape under protection; additional operations need reviewed exact adapters.

Source/API commit: 56d177954239211478e34408dc3788e25416893a.
Successful public source/harness head: 1a22fbfdccda44e6c418e1efd401d7dc665ee5c2.
Later documentation/receipt commits do not change its bytecode. Every commit's
author and committer were verified as Tyler Bush <tyler.jonathan.bush@gmail.com>.

## Verification

Commands ran in suifleet-dev:latest, image
sha256:c8b9afa8c874d5c49b65570ffff44596f2501dc7a22fbc0532d612e6a34f4c0d.
Offline chain/Move containers used --network none and private caches/configuration.
The verified 1.83.0 network artifact was mounted at /opt/sui-network/sui; the
release compiler remained 1.81.1. This verifies the installed runtime artifact,
not a full local rebuild of the updated Dockerfile image; ordinary CI must build
and check that image revision.

| Exact command (inside image unless stated) | Actual result |
|---|---|
| sui move test --path contracts | PASS 315/315, including 25 security cases and all 16 masks |
| sui move test --path contracts/fleet_storage | PASS 77/77, including exact WAL withdrawal/cap restoration, changed amount and missing factor |
| python3 scripts/check_move_security.py | PASS strict compiler/unsuppressed baselines, core regressions, storage77/data60, genuine failing authority mutations |
| python3 scripts/generate_interfaces.py --capture and --check | PASS 15 modules and synchronized TypeScript/Rust/recipe layouts |
| Separate catalog JSON comparison against 93cc5c3 | PASS every prior public signature, ordered layout/ability unchanged; no duplicate optional origins |
| node scripts/test_security_policy.cjs | PASS production parser/builders, legacy1/196 versus current197, masks/type/schema/network refusal |
| node scripts/test_customer_fleets.cjs | PASS existing discovery/authority/capability regressions |
| pnpm typecheck; pnpm build (dapp) | PASS static production settings build |
| make test-testnet (verified network binary mounted) | PASS 148 offline testnet + 61 shared-live regressions, authenticated cache tampering tests |
| cargo test --locked --offline (suifleet-edge) | PASS 462; 0 failed; 4 existing OCI tests ignored because Docker Engine/SF_OCI_TEST_IMAGE unavailable |
| cargo build --locked --offline | PASS debug daemon; private copied target cache, no other checkout target modified |
| python3 scripts/test_protocol_localnet.py --customer-upgrade | PASS real populated upgrade, old-package failures, current daemon signed the intended Failed receipt, gas/revocation/rollout/reassignment/removal |
| python3 scripts/test_security_policy_localnet.py --base /base/contracts --evidence docs/validation/sf196-localnet.json | PASS direct original-base to final bytecode, 16 effects-checked transactions |
| Actual production frontend readers against public fixture | PASS customer/fleet discovery and policy BCS read with mixed introduction origins; no wallet signing session |
| make test-ota | PASS entire debug-daemon/simulator suite, including all compact-history/quilt/retention modes; exit 0 |
| git diff --check, staged scope and commit identity | PASS |

The four ignored Rust cases are the existing Docker Engine OCI engine/profile,
helper reaping, supervision/recovery and successful-exit cleanup tests. They are
not claimed passed. Existing generated-field dead-code/deprecated vec_map test
warnings remain; no new security suppression was introduced.

Initial local invocations exposed missing container mounts/configuration, read-only
export/build paths, pubfile source-path mismatch, PTB vector/deposit argument errors
and compiler borrow/context errors; these were corrected before successful runs.
Initial make test-testnet attempts on the host failed because Compose was stopped
or the container-only identity test had no host sui; the actual container run
passes. Updated persist-callback mocks match the real shared helper. No failure
was relabeled as acceptance.

The direct upgrade used original base c4c0805c7abd8cfbfbf0d5e0df4a6f285b8da259.
[Local receipt](sf196-localnet.json): 1,377,453,147 MIST on a
disposable protocol140 chain. This is not a public-network price. The additional
customer upgrade harness used fdf3dd33c451f32e155a0bc768850087da8374ad and the
current real daemon, including checked chain state and the intended signed Failed report for its unavailable recipe. This proves authority/signing/effects, not artifact success. [Sanitized receipt](sf196-customer-daemon-localnet.json).
[Public frontend read receipt](sf196-frontend-read.json) verifies current197 and
Customer-at-base, Policy-at-intermediate, FleetSecurityKey-at-final origins.

## Protocol140 and public effects

The former 1.82.1 pin supports139 and correctly refused testnet before spending.
Official testnet-v1.83.0 reports sui 1.83.0-16d2dc1e05ed and upstream maximum140.
The downloaded Linux archive matched GitHub's published SHA256
e51168051bca14f1500de9a81520c927dfd583092628bc36582d01f9d2c1d7d8.
Extracted binary:
29e1beb96434d0d2e00642929dbab1d8813039b80c3eb514bbae2751fb2648fa.
Dockerfile/runtime pins and regression checks agree. The compiler and locked
framework, verify-before-sign, chain/account/authority, resource and spending
checks were retained. Bounded stdout diagnostics now expose unsigned build
failures as well as stderr; that repair cannot authorize signing.

The shared package reservation is now 850,000,000 MIST, with headroom above the
measured 751,037,763 MIST current-root upgrade. This is not a claim of a fresh
publication price: identical bytecode was not republished for measurement. The
run ceiling/balance and unknown-outcome reservations remain enforced; regression
cases explicitly clamp the larger reservation to remaining allowance, including
already-charged failed submissions.

All public runs used chain4c78adac, epoch1270/protocol140 and the dedicated agent
wallet. A public runtime YAML copy only resolved the existing container keystore
path on the host; no key/phrase/signing credential was copied into evidence.
Current cap owner/latest version/type origins were checked. Each run acquired and
asserted network:suifleet-testnet, used the shared non-replenishing ledger and at
most one faucet request, then promptly unlocked. Coordination is in #11. Only
isolated suite fixtures were selected; original application objects are intact.

First attempt: FAILED, 1,368,984,339 /
1,370,000,000 MIST, 13 transactions,
one faucet request. Public base storage exceeded the local estimate; the remaining
reservation caused a real InsufficientGas at catalog publication. The failure was
charged and the run ended. [Failed receipt](sf196-testnet-attempt1.json) preserves
independent read-backs for all 13 digests/statuses/net gas.

Separate repair: PASS, 772,390,658 /
1,000,000,000 MIST, 15 transactions, one faucet.
It reused the existing fixture and upgraded only its current latest version.
This is a staged public repair, not a claim of a direct original-base-to-final
public upgrade. The direct pair was tested on the isolated chain; normal
exact-head/current-base hosted acceptance remains a merge gate.

- Original base: 0xf8a1805c865b883b84a8b561c13024f9bb50e0bc3975da2a702a4ef1bebe7116.
- Intermediate/security-type introduction: 0x63fa6486ed899d45ac3410724513c05bf977aa6b8dd97171ef280879f9b999c7.
- Final/latest package, cap version3: 0xe153c384bbad616b5e415a119bd6a453fd6c71acb1fad5ae843ecbd9b71b6c06.
- Final bytecode: bytecode-v1:5aac7fbba41991f94a661f7e6a4474d8cdc062a68d12a85590e69025e916b9c8.
- Customer197 and UpgradeCap3 were independently read after completion.

Every repair digest, successful/failed effect and net gas was independently read
back and matched against the ledger. [Successful receipt](sf196-testnet.json)
records checkpoints, full lineage, actual per-type origins and verified tooling.
Expected aborts are acceptance; the prior unexpected failure remains failed.

| Repair check | Digest | Effects | Net gas (MIST) |
|---|---|---|---:|
| repair latest security fixture | 6gfxWNKWhMNNiCKd5X9A7Fn54zuLat4Yau6kmhkMeme5 | success | 751,037,763 |
| initialize existing customer policy | 4UvEe8UmAvCUVyKKg1n3gSAvi1h4XKmYrnRxyLgVZa2Q | success | 1,000,535 |
| seal reconciled fleet | CVbNNxpcySwm2WD4D7gcaGXxwfQTqQy44GToyNAPCPaN | success | 3,044,693 |
| old package set_credential | 6hY3gZW5ze8hKC3k9fN3VErx2Ve7TV2JSeiA7ABMzjjS | failure | 1,000,293 |
| old package rename_fleet | FS5CgiatKFMs4jUQn2C2kiXkeDqwaNeUKG6C5UVSb711 | failure | 1,000,210 |
| old package borrow_budget_cap | 4tjXtrvXVD5BpjmrBHhJew9MPDMoWtyCdnmUxia2AdKc | failure | 1,000,210 |
| intermediate package cannot alter administrators | ATrBjsHv5yFZ8PgaSfLdW7HAzMBzjWiHwfVocRcbRKzX | failure | 1,000,293 |
| unimplemented factor cannot activate | Bt3e4JDpPMYcLrq55jAm7QcLKZSW4Jb889prmfy25Lff | failure | 1,000,293 |
| current all-off administration | 58B3EpTVEfzM3oe293zW5nSA2ypuxrqLncNhsbnT6PTz | success | 1,000,210 |
| create disposable catalog | FA82STfEB73BTnMKgdJTSTV8gydRX5cmbcG1KyMeNuJj | success | 2,694,800 |
| customer takes publisher custody | 4oF6mJ4eZWukgaTRkSc2ezuV91H4FPfQZmr4RKFgV81t | success | 1,000,169 |
| old catalog publisher excluded | AwqTSVrSbFz2KHDmrqRfKFeEiz4WNXu4E7fwgpJCt3BE | failure | 1,000,169 |
| customer publishes authenticated version | 4fBZFKWL3s9MqY27iUuMbVzMkKDQN7P5sNksmxv446Tb | success | 4,610,169 |
| fund isolated activity budget | BZfg3umEf4iZtEDmtECtaaQyTDphErKirrhNRzFPjoU1 | success | 12,580 |
| scoped activity withdrawal | Eb8ESVzTZvLX5VsqNkYndDQyHUZNmSw4UcZvGUGQevsf | success | 1,988,271 |

## Cached public reuse and conservative funding

The final harness commit 3e8889f83f76fce3ca642cb5346ef95b53c30dad adds a conservative funding reservation:
the 1,000,000 MIST fixture budget deposit remains charged to the allowance even
when withdrawal returns it. It cannot replenish the run. The earlier repair
receipt reports its actual original ledger; it is not rewritten as a later run.

A separate cached run passed **14 effects-checked transactions**, with **no
publication/upgrade and no faucet**. Gas/balance spend was
19,308,253 MIST; the non-replenishing allowance charged
**20,308,253 / 100,000,000 MIST**, including that
1,000,000 MIST funding reservation. All 14 digests/statuses/net gas were
independently read back. Custody and publisher refusals still hold. The root
bytecode hash is identical across this cached run, staged public repair and
direct local base-to-final receipt. The wallet lock was released promptly.
[Cached receipt](sf196-testnet-cached.json).

## Actual production Action API

A final dedicated cached-fixture run at source/harness 276131444a289e3cee8ca514c2dce3d29f802d5b passed five
transactions, with no faucet or publication. Actual wallet spend:
5,032,316 MIST; conservative allowance charged **7,439,127
/ 100,000,000 MIST**. Storage refunds did not replenish its allowance.
The wallet lock was promptly released. All five effects/net gas were independently
read back. [Action receipt](sf196-security-actions-testnet.json).

Production SDK object BCS supplied canonical customer/fleet/name arguments;
the Move domain helper produced the intent. The shared Action executed the exact
rename and was deleted. Changing the name aborted in begin_scoped_bytes and left
the fleet unchanged; transaction rollback retained that Action, which a current
administrator then cancelled/deleted. These are real default/all-off framework
execution/tamper/cancel effects, not simulated factor proofs or completed child
mechanisms. Deletion prevents reuse of the same nonce.

| Action check | Digest | Effects |
|---|---|---|
| prepare canonical action: SF196 intent-bound fleet | Ek7Z6Po9t3bD9WSqrxDoSpXtzEARkVBSKAtucAX8tUiF | success |
| execute exact single-use action | DYbr2HEu6FgYdRkgeMMFno26hWXqswzSys8UmxUqPG31 | success |
| prepare canonical action: SF196 approved name | AvThKbsdXGpSJzxEYi32GTejd6hovf8opGUrbA4zADYo | success |
| substituted arguments rejected on chain | EitgCUNZ8ui6MKcyxGCAnvTJfxQQyye6bGQUtaUQJNTv | failure |
| cancel retained action after failed substitution | hJ87ZXE2wvgAzJ6uxvWwjDnmF5M75SFeoyxECn8pLFw | success |

## Limits

No passkey verifier, multisig ceremony, second-person workflow or delay queue is
claimed complete. The reviewed framework/API is their prerequisite; source and
upgrader governance remains a separate explicitly trusted boundary.

No manual extension-wallet/outsider/provider, physical, production or mainnet
acceptance occurred. Retain #32/#159 and hosting acceptance #62. No application
rollout or physical programming was authorized. Hosted CI has not been watched or
polled. Pushed-head ordinary/security/testnet checks and fresh independent review
remain pending coordinator merge gates; pending checks are never approval.
