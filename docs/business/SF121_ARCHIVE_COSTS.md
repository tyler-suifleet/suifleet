# Archive retention and cleanup experiment (SF-121)

Measured 2026-10-23 on dedicated Sui testnet chain `4c78adac`, with synthetic
2 KiB payloads, real Walrus node writes/certification/readback and executed Sui
effects. These are testnet SUI, not mainnet prices or a subscription margin.
[Raw effects and transaction digests](../validation/sf121-cost-controlled.json).
No mainnet spending, customer fleet mutation, Pi programming or new server.

## Equal-retention comparison: remove prepaid renewal machinery

Three separate fleet-storage fixtures each had four storage slots; two uploads
per profile distinguish first-upload initialization from recurring costs. Finite
storage initially bought 3 epochs; retained storage bought 3 plus prepaid renewal
for one more. The matched finite-upfront profile bought 4 epochs immediately.
The latter two share target expiry epoch 563 (registration epoch 559).
The comparison does not reduce the purchased retention promise.

| Per archive, SUI | Finite upfront, first | Finite upfront, recurring | Retained, first | Retained, recurring |
|---|---:|---:|---:|---:|
| Registration | 0.012297565 | 0.009212273 | 0.019655095 | 0.016569827 |
| Certification | 0.001030858 | 0.001030858 | 0.001030858 | 0.001030858 |
| Owner cleanup, net | -0.007229553 | -0.007229553 | -0.014584863 | -0.014584888 |
| Lifecycle subtotal | 0.006098870 | 0.003013578 | 0.006101090 | 0.003015797 |

Removing per-blob prepaid renewal machinery reduces recurring registration cash
outlay **44.4%**, or 0.007357554 SUI. After cleanup the lifecycle difference is
only **0.000002219 SUI** (about 0.074%). Most of the upfront reduction is less
refundable capital tied up, not an equivalent reduction in permanent fees.
Finite 3-epoch and 4-epoch fixtures had the same Sui effects costs; WAL purchases
remain separate and the extra retention is not free.

## Cleanup gain and its limits

For recurring finite archives, registration plus certification consumed
0.010243131 SUI before cleanup; net owner cleanup reduced that to 0.003013578
SUI, returning about **70.6%** of that cash outlay. The net cleanup figure already
includes its computation fee. WAL storage/write payments are not refunded.

These disposable blobs were explicitly released/burned by their owner immediately
after readback. This measures rebates; it does **not** implement unattended
expiration cleanup. Production cleanup must wait until the purchased retention
ends. The live BlobRecord reader rejects released archives even if Walrus still
has bytes. Account/fleet setup, unused reserves and remaining fixture objects
are not claimed to have been cleaned up.

For 1,000 devices and 30 uploads each, applying one first-upload cost per device
and 29 recurring costs gives about **93.49 testnet SUI** for these three lifecycle
steps. This excludes WAL, data anchors, health, deployment receipts, setup,
retries, providers and production operations. It is not a total monthly bill.
The earlier roughly 812-testSUI extrapolation reused first-upload costs and kept
storage deposits outstanding; it is not a permanent-fee estimate.

## Bounded history implementation and validation

The additive `anchor_compact` API and native/browser schema-3 history chain are
implemented; see [configuration and limitations](../SERVERLESS_FLEET_DATA.md#opt-in-bounded-archive-history-sf-121).
This bounds the data-history index, not per-blob storage objects. No transaction
consolidation or Quilt aggregation is included in this change. Health defaults
to daily for new fleets; existing fleets keep their configured cadence. Logs
remain opt-in, with zero uploads in the default budget estimate.

Validation completed: 21 Move tests; 397 Rust tests passed with 4 existing ignored;
legacy and compact real-daemon OTA simulators; actual native Seal encryption to
browser decryption/signature/history verification across four archives; shared
Rust/TypeScript BCS golden vector; tamper/replay/signer-rotation/bounds controls;
frontend typecheck and production build with pinned Node 22; interface/manifest
and offline tracking checks. These results do not establish physical Pi or
mainnet acceptance.

The preserved probe subsequently completed two synthetic registrations,
certifications and compact root writes. Their six executed effects and exact gas
values were independently re-read on 2026-10-23, and the application reader
rediscovered the second root. [Effects, digests and both root readbacks](../validation/sf121-compact-effects.json)
supersede the earlier incomplete summary; earlier quorum failures remain recorded.
No new publication or purchase was needed for this reconciliation.

| Synthetic 2 KiB upload, testSUI | First | Second |
|---|---:|---:|
| Registration | 0.009212273 | 0.008847473 |
| Certification | 0.001030858 | 0.001030822 |
| Compact anchor | 0.004617999 | 0.001304761 |
| Subtotal before cleanup/WAL | 0.014861130 | 0.011183056 |

The second upload was an emergency-kind archive, whereas the first was routine.
This is a measured pair, not a controlled percentage reduction against legacy
anchors or proof of constant future cost. Registration/certification and health
writes remain separate transactions. The compact root bounds discovery state;
per-blob storage still grows until cleanup. The two compact samples have not
been released and their actual cleanup cost is not included in this subtotal.

This public probe used an isolated audit installation of Walrus SDK **1.2.36**
and Sui SDK **2.37.0**. The application remains pinned to **1.2.35 / 2.36.0**.
The synthetic roundtrip does not establish the required pinned-SDK Quilt path,
confidential owner/support access, or current-head upgrade compatibility. The
published package also predates the TxContext lint repair. Full SF-121 acceptance,
including device-local quilting, remains outstanding; this evidence must not
close the issue or establish subscription viability.

The resumed [disposable-chain upgrade harness](../validation/sf121-localnet-upgrade.json)
passed with current Move source: existing-object bytes were preserved, later
introduction origins remained distinct, diagnostics worked on an existing writer,
and both old/new calls refused a revoked device. This closes that specific local
compatibility check, not pinned Quilt or public current-head acceptance.

A fresh [read-only reference gas observation](../validation/sf121-reference-gas.json)
verified Mainnet chain `35834a8a` at 100 MIST and Testnet chain `4c78adac` at
1,000 MIST per gas unit. This is not a mainnet transaction quote: storage charges,
rebates, WAL and provider charges do not scale by that ratio. No mainnet write or
subscription-margin conclusion follows.

## Pinned device-local Quilt geometry

`node scripts/measure_quilt_geometry.cjs` uses the application's exact Walrus
1.2.35 encoder and Quilt patch reader, with an explicit in-memory byte source.
It roundtrips every item by patch ID, covers 2 through 666 items, and rejects
667 items at 1,000 shards. [Machine-readable results](../validation/sf121-quilt-geometry.json)
include the experiment source hash. This establishes local encoding/patch
geometry, **not** a storage-node/certification or confidential archive roundtrip.

| Synthetic ciphertext items | Packed payload bytes | Quilt blob bytes |
|---|---:|---:|
| 2 × 256 B | 512 | 445,556 |
| 8 × 2 KiB | 16,384 | 445,556 |
| 64 × 2 KiB | 131,072 | 445,556 |
| 8 × 64 KiB | 524,288 | 891,112 |

These are blob bytes before erasure-coded sliver expansion. The packed column
omits application framing and is a geometry comparison, not a complete encrypted
archive invoice. Random bytes model ciphertext lengths; this experiment neither
compresses nor encrypts data. The minimum quilt padding is substantial for small
device-local batches. The 666-item × 256-byte case produces 7,128,896 blob
bytes, exceeding the current 4 MiB archive limit; item count alone is not
a sufficient bound. Splitting an existing packed archive into Quilt patches
adds per-item framing/encryption overhead and does not itself remove a transaction.
The current daemon already packs many log records before encrypting one archive.

For an explicit 28-day, 1,000-device comparison with no retries and one health
write per device per day:

| Archive cadence/design | Registration | Certification | Data anchor | Health | Total transactions |
|---|---:|---:|---:|---:|---:|
| Daily current packed archives | 28,000 | 28,000 | 28,000 | 28,000 | 112,000 |
| Daily per-device quilts | 28,000 | 28,000 | 28,000 | 28,000 | 112,000 |
| Weekly packed archives | 4,000 | 4,000 | 4,000 | 28,000 | 40,000 |
| Weekly per-device quilts | 4,000 | 4,000 | 4,000 | 28,000 | 40,000 |

This count excludes setup, renewal, cleanup and deployment receipts, which remain
separate. It assumes one resulting blob per device per archive interval, not an
unbounded patch queue. Retaining six weekly archives requires 6,000 live blobs
for 1,000 devices before cleanup; the existing protected-blob capacity and expiry
cleanup limits must be accounted for (tracked separately in SF-128/#258 and
SF-129/#264). Longer batching reduces freshness; retaining health authentication
keeps all 28,000 health writes. Retrying a node transfer does not require another
registration; a failed executed transaction still incurs its actual gas and is
not free. Neither a cross-device quilt nor shared upload transactions are
implemented or assumed: those need a data rendezvous outside this design.

The geometry therefore supplies no evidence that substituting device-local
quilts for the existing packed archive improves the measured operating envelope.
It does not waive the required runtime implementation or demonstrate the example
subscription price is viable. Prices/profitability need the complete measured
workload, cleanup and current production quotes; no testnet-to-mainnet divisor
or fixed subsidy is applied here.

## Reproduction and spending controls

`scripts/measure_archive_costs.py --help` describes the dedicated testnet probe.
Supply the verified core, registry, UpgradeCap, storage package, explicit agent
wallet, pinned SDK and build/network CLIs. Acquire `network:suifleet-testnet`
first. It publishes no packages, never uses a faucet, checks authority/chain and
records effects after each operation. Resume only with the original evidence
and identical fixture/allowance; uncertain outcomes require reconciliation.

The entire operator experiment, including compact-package preparation, used one
non-replenishing 0.5-SUI allowance. Refunds and faucet funds never replenish it.
Parser refusals are retained in evidence and their conservative reservations
were not restored. No keys or recovery material are in the evidence. Public
network availability is an integration limit, not a reason to fabricate success.

Mainnet reference gas observations cannot be applied as a blanket divisor:
computation, storage deposits, WAL, setup and provider costs must stay separate.
No final subscription price, payment implementation or numerical margin is
established. The agreed product direction is a device-tier platform subscription
with purchased status cadence, bounded optional logs and diagnostics allowances.
