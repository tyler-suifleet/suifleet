# Resumable fleet cost collection (SF-155)

This prepares the [SF-135 two-week bill acceptance](https://github.com/tyler-suifleet/suifleet/issues/267).
It does not authorize a Mainnet workload, buy tokens, select paid providers or
establish prices. SF-135 and [SF-087](https://github.com/tyler-suifleet/suifleet/issues/159)
remain open. Physical and real-wallet acceptance stays in
[SF-031](https://github.com/tyler-suifleet/suifleet/issues/32).
These tools accept no wallet/signing configuration and submit no transactions.

## Run and trust boundary

The runnable [smoke manifest](sf155/smoke-run.json) embeds the existing SF-082
[deployment manifest](../DEPLOYMENT_MANIFEST.md), including call package, per-type
origins, registry, chain and client ranges. It describes a historical one-device
observation, not a new pilot or current deployment certification. Its code/workload
revisions identify that historical fixture. Do not substitute it for a verified
production manifest.

For a future run, copy the structure and freeze these fields before observation:

- schema_version (1), unique run_id, fleet_id and provenance;
- full deployment interface and canonical accounts covering operator, devices,
  gas pools and WAL reserves being consolidated;
- exact 40-character revisions.code and revisions.workload;
- workload.devices, shared_releases, report_seconds, upload_seconds,
  retention_epochs, poll_seconds, max_transactions and max_records;
- timezone-qualified interval.start/end (at most 31 days), and all
  required_categories from fleet_costs.CATEGORIES.

Positive cadence values describe planned behavior; they never fabricate observed
counts. Actual devices, byte sizes, failures and traffic belong in evidence.
Account/workload/deployment changes require a separately reviewed run.
The optional transactions field holds a frozen smoke inventory. A growing pilot
inventory can instead be the reader's third argument: a JSON object containing
transactions, a list of objects with digest and category. The reader preserves the
inventory in each observation batch. Retain authenticated runner receipts/device
journals and checkpoint ranges; this reader does not discover all transactions or
prove that an inventory covers an interval.

Only sources may be appended to the durable run. Each entry has path (relative to
the run file, or absolute), sha256 of exact bytes, provenance and approved_by.
Keep the manifest in a trusted, access-controlled/versioned evidence directory.
Independently authenticate and review source hashes (HTTPS gRPC capture, signed
runner evidence or authenticated provider exports) before pinning them. A hash
prevents later substitution; a hash or free-text approver alone does **not**
authenticate an untrusted file. Do not auto-approve arbitrary downloaded evidence.
The operator authenticates origin; the collector validates integrity and scope.
Never put credentials or device keys in evidence.

## Capture, pin, ingest, resume

Use the app's locked SDK installation, or this isolated exact audit dependency;
it does not change application dependencies:

    npm install --prefix /tmp/sf155-sdk --ignore-scripts --no-audit --no-fund --save-exact @mysten/sui@2.36.0
    node scripts/observe_fleet_costs.mjs /tmp/sf155-sdk docs/business/sf155/smoke-run.json > /tmp/observation.json
    sha256sum /tmp/observation.json

After authenticating/reviewing the capture, append its path, hash, provenance and
approver to a COPY of the run's sources. Preserve earlier pins. For a pilot, pass
the growing inventory JSON as the reader's third argument. Ingest with:

    python3 scripts/fleet_cost_collector.py /evidence/run.json --database /evidence/run.sqlite > /evidence/report.json

Repeat against the same database after appending sources. Use one bounded
capture/ingest per poll_seconds with an operator-controlled scheduler; no daemon
or service is installed. Write capture/report output to temporary files and rename
only on successful exit; a failed command's empty stdout must not replace good
prior evidence. A read failure creates an unknown digest, never a free success or
a transaction retry. Chain mismatch, malformed effects and inventory overflow
refuse output. Reobserve the same unresolved digest until settled.

SQLite FULL synchronization and one write transaction bind source checkpoints,
records and history together. Process death rolls back the batch. Repeated digests
charge once; unknown outcomes can advance to settled effects with audit history.
Conflicting settled effects and renamed duplicate economic events refuse ingestion.
Each invocation rechecks hashes even for consumed sources. Back up using SQLite's
backup API, or while stopped, together with run/source files.

Reproduce the already pinned smoke offline:

    python3 scripts/fleet_cost_collector.py docs/business/sf155/smoke-run.json --database /tmp/sf155-smoke.sqlite > /tmp/sf155-report.json
    python3 scripts/fleet_cost_collector.py docs/business/sf155/smoke-run.json --database /tmp/sf155-smoke.sqlite > /tmp/sf155-resumed.json
    cmp /tmp/sf155-report.json /tmp/sf155-resumed.json
    python3 -c 'import json; d=json.load(open("/tmp/sf155-report.json")); json.dump(d["accounting_model"],open("/tmp/sf155-model.json","w"),indent=2)'
    python3 scripts/fleet_costs.py /tmp/sf155-model.json --require-complete

The last command intentionally exits 2: a Testnet smoke is not a Mainnet invoice.

## Supplied observation interface

Each batch contains network, chain_identifier, run_id and records. Every record
has type, unique id, timezone-qualified at within the run and source identifying
authentic raw evidence. Append facts; do not rewrite settled records. Amounts are
integer base units; missing amounts are null.

| Type | Additional fields and interpretation |
|---|---|
| transaction | Digest as id; category; status unknown/success/failure. Settled records require sender, gas_owner and gas (computationCost, storageCost, storageRebate). The gRPC adapter retains checkpoint, balances and raw response. Unknown records have no settled cost. |
| walrus | digest, blob_id, operation (purchase/renewal/reclaim), category, encoded_bytes, actual start_epoch/end_epoch, frost (consumed WAL or null). Supply disjoint allocations per transaction/blob/operation/category with raw purchase/event proof; never duplicate a bundled purchase across blobs. |
| requests | basis (measured/estimated/unavailable), requests, download_bytes, provider_usd. Unique IDs delimit provider/window/object scope. Unavailable counts must be null. Retain failed requests, cache behavior, geography and response evidence. Counts never imply a tariff. |
| balance | account, unit (MIST/FROST), boundary (start/end), amount. Exactly one boundary per account/currency. Include object-held fleet funds at common checkpoint boundaries. |
| transfer | digest, from, to, unit, amount. Link settled successful effects. Internal operator/device/pool funding cancels; external acquisition/funding and withdrawals remain explicit. |
| capital | object_or_account, kind (refundable_storage/gas_float/wal_reserve), unit, amount. Snapshot at at, never expense or a sum of snapshots. Link actual rebate/reserve reads. |

For a renewal, record purchased epochs/encoded bytes and the settled transaction
plus its authenticated WAL charge. If charge or effects are unavailable, keep
frost null; it remains incomplete. Reclaim records describe the actual interval
and observed WAL charge (often zero), never an invented WAL refund. Record actual
SUI cleanup rebates through effects. Bought epochs do not prove future wall-clock
availability; preserve ownership, eligibility and renewal liabilities separately.

## Accounting and subscription integration

The accounting_model output feeds [fleet_costs.py](../../scripts/fleet_costs.py)
and the [fleet cost model](FLEET_COST_MODEL.md). Settled failed transactions charge
actual gas. Signed net gas is computation plus storage minus rebate; the
informational nonrefundable field is not added twice. Only gas paid by consolidated
accounts is fleet expense; sponsored effects remain visible separately. Internal
funding cancels and external funding is liquidity. Whole-fleet MIST/FROST boundary
reconciliation reports residuals. WAL costs need successful linked effects and
authenticated purchase evidence, never budgets. Do not book the same WAL purchase
as both a balance delta and a separate expense.

The collector deliberately never certifies a complete Mainnet bill: interval
coverage, invoice scope, capital lifetimes and all required services need final
SF-135 review. subscription_input remains null. Request counts and supplied USD
charges remain auditable observations, not automatically booked invoices.
Missing services/outcomes/counters/balances remain explicitly incomplete.

For [subscription planning](SUBSCRIPTION_PRICING.md), review complete Mainnet
recurring consumption and timestamped FX before populating sui_sensitive_usd,
walrus_usd and services_usd. Remove held refundable capital and gas float from
recurring expense and report them under working_capital. Do not subtract
speculative future rebates. Separate one-time setup. Label any 14-day-to-month
projection as estimated with explicit retention/workload assumptions. Missing
required services stay in unquoted_costs so pricing refuses a quote. Never
rescale or FX-convert Testnet quantities into measured Mainnet expense.

## Final SF-135 two-week procedure (not executed)

1. Obtain separate authority for Mainnet writes, paid services and token purchases.
   Verify approved chain, accounts, package/capability/manifest and runtime
   revisions, service terms, workload bounds, allowance and stop conditions.
   This preparation grants no physical programming or production authority.
2. Before day 1, freeze a trusted run for exactly 14 days, identities and maximum
   operations. Record initial checkpoint, SUI/WAL balances, capital objects,
   prepaid capacity, encrypted byte hashes/sizes, codec/provider versions and
   request counters. Keep setup outside the recurring sample with separate evidence.
3. The authorized workload runner must preserve every attempt/digest, failure,
   unknown outcome and funding receipt. Maintain its authenticated inventory.
   At every poll interval, capture with the read-only adapter, authenticate/pin
   the batch and ingest into the same journal. Never resubmit unknown writes.
   Bounds stop collection for reconciliation; do not silently drop observations.
4. Daily, authenticate request/download/provider exports and WAL purchase,
   certification, renewal and reclaim events. Record actual epochs/capacity,
   sponsorship, exchanges, external acquisition costs and object/pool balances.
   Compare runner inventories with checkpoint/account histories to find omissions;
   a digest reader alone cannot establish full interval coverage.
5. At day 14, stop the separately authorized workload; take common final
   checkpoint/balances/counters/capital. Settle outstanding digests by reads.
   Record later settlement, expiry, cleanup and renewal liabilities in a separately
   labeled tail interval. Do not invent refunds or free retention.
6. Reconcile token residuals and all unknowns. Review device/report/upload/download
   coverage, failures, retention, availability and required services. Obtain
   authentic invoices or applicable free-tier evidence. Missing required charges
   remain incomplete. Publish sanitized evidence/hashes, effects, actual costs,
   capital and limits in SF-135; keep credentials private and SF-087 open.
7. Export the accounting model and reviewed recurring subscription input. Report
   the actual 14-day bill separately from monthly projections. Real prices and
   comparative savings remain unmeasured until this acceptance actually runs.

## SF-155 validation evidence

Codex, actual configured/running model gpt-6.1-sol, observed existing SF-082 digest
7tbT2SNG2JFeSrnKEtCnWC9Ads7MBq28FsNPZvXSXZWN with SDK 2.36.0 gRPC v2.
The [capture](sf155/smoke-observation.json) records verified chain 4c78adac,
checkpoint/time, sender, payer, effects and balances. Net gas is 3,387,716 MIST;
no new fixture, transaction, upload or faucet request occurred. The manifest
preserves historical fixture identities, not current package certification.
Reingestion produced byte-identical reports and one booked transaction.

    python3 -m unittest discover -s scripts/tests -p 'test_*fleet_cost*.py' -v
    python3 -m unittest discover -s scripts/tests -p test_subscription_pricing.py -v

The offline tests cover positive/failed/unknown effects, crash/restart rollback,
duplicates/conflicts, integrity/network/SDK mismatches, bounds, funding, WAL
retention, missing provider charges and capital separation. No Mainnet expense,
two-week run, production service, real-wallet or physical result is claimed.
Hosted CI and independent review remain required.
