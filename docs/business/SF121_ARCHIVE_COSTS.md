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

The new compact package was published once on dedicated testnet and its module
bytes were checked against the pinned build. The additional synthetic blob was
registered once; storage-node writes failed to reach quorum (fetch failures and
some nodes reporting registration unavailable). Retries reused the registered
blob and did not purchase it again. Consequently **live compact anchor cost and
its percentage saving are not measured yet**. No successful certification/root
write is claimed. The implementation retains simulator evidence and awaits
public integration review/acceptance. The existing SF-121 issue remains open for
its broader economics/Quilt scope rather than being closed by these partial gains.

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
