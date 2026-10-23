# SuiFleet: network-first business plan

Decision basis: 2026-10-16. This records Tyler's original commercial thesis, not a newly discovered positioning idea. Product implementation and hardware acceptance remain separate from business hypotheses.

## The opportunity

SuiFleet can potentially replace recurring fleet-platform charges with low marginal network usage: publish an authenticated release once, distribute the same artifact across many devices, and pay for authority changes and execution receipts. The opportunity is to pass a substantial share of that infrastructure saving to customers while retaining a margin on measured usage. Cost advantage is part of the product, alongside deployment control and independently verifiable records.

Do not begin with an arbitrary per-device SaaS subscription or use assumed engineering labor to manufacture savings. Establish the network-only bill first. Separately measure any paid delivery, RPC, indexing, hosting and key services needed for the chosen operating model; optional managed hosting and support can be distinct offers. No subscription price, production saving percentage or gross-margin claim is established here.

A fleet with many devices and relatively infrequent releases is the initial economic hypothesis. Idle devices do not themselves require a periodic Sui transaction, although retained objects, active integrations and selected services can have costs. Receipts still scale with device activity: one shared artifact does not make 1,000 device reports one transaction.

## Customer offer and product boundary

Start with embedded Linux manufacturers seeking a Greengrass-like deployment/component-management service. Offer explicit artifact retention and metered deployment/reporting usage, with customer-funded or sponsored network payments. Provide estimates and spending caps before writes. A customer-operated agent/dashboard path and optional managed service avoid making an arbitrary subscription the only way to use the product.

SuiFleet verifies and delivers artifacts, executes configured lifecycle commands and records supplied results. Platform scripts/tools own OS installation, partition/bootloader layout, system reboot, boot confirmation and firmware rollback. A signed receipt proves a report by the signing identity, not independently that a physical device booted correctly or remains uncompromised.

The pitch is: **manage deployments at a price derived from actual usage, preserve the customer's platform integration, and make release authority and reported outcomes independently verifiable.** Full Greengrass API/recipe/component compatibility is a roadmap, not an existing sales claim.

## Mandatory customer confidentiality

Seal-encrypted delivery and scoped account/device/group decryption are required for proprietary customer readiness in SF-022/#25. Encrypt before any publisher/relay upload; explicit public artifacts remain available for public smoke tests. Decryption privileges do not grant publish/deploy/admin authority. Devices authenticate with their own enrolled signer; administrator wallet secrets are never distributed to devices. The current plaintext OTA path does not satisfy this gate. See [wallet delegation and edge authentication](ACCESS_AND_ENCRYPTION.md) for implemented versus planned behavior, bootstrap credentials and revocation/cache limits. Production Seal provider fees and encrypted-delivery costs remain to be measured.

## Evidence and network-only example

The [public Mainnet snapshot](business/network-costs-2026-10-16.json) records read-only queries, raw fee values, workload, arithmetic and exclusions at 04:34 UTC. It contains no keys and submitted no transactions. Its derived costs are estimates from live parameters, not a paid production invoice. [Walrus network reference](https://docs.wal.app/docs/network-reference) identifies the system object, token units and relay endpoint.

Workload: 1,000 devices receive four shared 500,000,000-byte releases; four blobs retained for two 14-day epochs; one receipt per device/release. Using the documented approximate encoding factor and metadata gives 2,207 encoded MiB per blob after rounding. Exact SDK encoding and storage-boundary epochs must still be verified. See [Walrus cost model](https://docs.wal.app/docs/system-overview/storage-costs).

| Component | Snapshot-derived amount | Evidence boundary |
|---|---:|---|
| Artifact storage, two epochs | 0.831050264 WAL | Raw price × encoded MiB × epochs |
| Four artifact writes | 0.755208916 WAL | Raw write price × encoded MiB |
| Selected upload relay | 0.36156256 SUI | Its observed linear per-KiB tip |
| Minimum computation component for 4,000 separate receipts | 0.4 SUI | Minimum computation only; excludes receipt storage and actual execution buckets |

Thus the identified components are **1.586259180 WAL + 0.76156256 SUI**, not the complete fleet bill. At reference quotes used in the preceding discussion (WAL $0.04589998 and SUI $1.034), those components convert to approximately $0.86. These reference quotes were not refreshed in the snapshot and must not be presented as current exchange rates.

The complete bill still requires deployment/targeting objects, recipe blobs, Walrus reserve/register/certify gas and deposits, receipt storage, onboarding, retries, additional reports and cleanup. Retention/refunds affect cash required versus permanent expense. Do not treat gas budgets or device gas balances as consumed fees. Historical [Testnet receipt evidence](validation/sf043-testnet.json) is useful for transaction structure, not a Mainnet price quote.

The raw WAL-priced snapshot has not been reconciled with the documentation's advertised USD storage approximation. Any subsidy, token conversion, exact encoding and expiry-boundary effects must be resolved before issuing a customer quote. Public reads/downloads have no per-read Sui write fee, but this does not establish a free, unlimited or SLA-backed distribution service. Quote paid gateways/RPC separately when selected.

For comparison, AWS's example Greengrass rate is $0.16 per active core-month: $160 for 1,000 devices, before other services. This demonstrates an avoidable recurring charge, not a proven complete-service saving or equivalent availability guarantee. [AWS Greengrass pricing](https://aws.amazon.com/greengrass/pricing/).

## Pricing discipline and validation

Use an auditable usage model: network fees + separately metered selected infrastructure + an explicit commercial markup. Whether markup is a percentage, operation fee or an allowance is undecided until complete bills are measured. Exclude labor from the base cost comparison; price requested integration/support separately. Do not assume percentage markup on cents will fund a company: validate fleet scale, paid usage and optional service demand.

Before quoting a production fleet, measure a representative release end to end; count actual transactions/bytes and successful/failed effects; capture storage deposits/refunds, renewal and upload fees; verify delivery at expected concurrency; account for selected service charges; and compare the same workload, retention, security and availability against the customer's actual AWS configuration. Test Mainnet simulation/estimation without publication where possible; live Mainnet spend still requires explicit user authorization. Validate purchasing demand alongside the technical prototype.

**Platform subscription decision (2026-10-23).** The user selected fixed platform subscriptions with device-capacity tiers and purchased reporting settings, rather than usage invoices. New-plan defaults are daily status and disabled log uploads; enabling logs selects an upload interval, bounded bytes, retention and a diagnostic/crash allowance. Show the price before plan changes; do not generate automatic overage invoices. The $100/month for 1,000 devices example is a modeling scenario, not an approved price or demonstrated margin. Payment method, final prices and subscription collection/enforcement remain unimplemented decisions. **Current prepaid network costs (SF-083):** the fleet budget still collects network funds only and charges no SuiFleet service fee. Customer deposits are not subscription revenue. Sponsored transactions through a gas station, and Nautilus-enclave sponsorship, are recorded as future options, not the current design: both need an always-on signer or self-managed enclave hosting. See [FLEET_BUDGET.md](FLEET_BUDGET.md).

## Adjacent opportunities, reconsidered

Rank by avoidable customer charges, network fit and implementation burden, not invented AWS service margins. Each row is a product hypothesis requiring its own measured bill and acceptance scope.

| Order | Product | Economic reason to investigate | What still needs validation |
|---|---|---|---|
| 1 | Fleet/component deployments and signed release distribution | Reuse one release across the fleet; replace active-device rent with deployment/report usage; closest to existing code | Complete receipt/storage/delivery costs, scale, recovery and migration compatibility |
| 2 | Configuration history, policy and change approval | Paid configuration-change metering can be much higher than small authority/commitment operations; share history across organizations | Collectors, real drift detection, rule evaluation, searchable index and provider integrations |
| 3 | Encrypted configuration and selected secret distribution | Potentially avoid recurring per-secret charges for small, infrequently changed data | Appropriate data sensitivity, production key-service price/availability, rotation and recovery; no blanket KMS replacement |
| 4 | Signed artifact registry and cross-company data distribution | Reuse SuiFleet's manifests, hashes, authority and Walrus artifacts beyond devices | Paid distribution/egress behavior, SDK/API compatibility, retention, confidentiality and availability |
| 5 | Just-in-time access authorization and delegated operations | Replace selected per-node access charges with explicit grants/revocations and audit records | Endpoint enforcement, identity federation, secure sessions/relays and revocation semantics |
| 6 | Batched audit/provenance records | Amortize an onchain commitment across many events; verify records across organizations | Event source trust, retention, indexing/query cost and equivalent coverage; never put each ordinary log event in a transaction |

AWS examples make the second and third rows worth investigating. Config continuous recording is $0.003 per configuration item in its US East example, or $3,000 per million items before rule evaluation. Secrets Manager's 10,000-secret example is $4,000/month for secrets plus $60 for its specified calls. These are customer charge pools, not estimates of AWS profit or quotations for a replacement. [Config pricing](https://aws.amazon.com/config/pricing/), [Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/).

For just-in-time node access, Systems Manager currently lists $0.0137/node-hour for the first 72,000 hours, with lower subsequent tiers. This feature's pricing is not a fee for every Systems Manager operation, and older advanced-instance prices must not be advertised as its current general pricing. [Systems Manager pricing](https://aws.amazon.com/systems-manager/pricing/).

Seal is a candidate for suitable encrypted configuration/distribution, not permission to sell highly sensitive key custody or regulated-data storage. Its documentation explicitly excludes some such data and distinguishes it from AWS KMS. Mainnet key providers must be selected and priced; test infrastructure is not a production SLA. Revoking future access cannot undo data already decrypted. [Seal scope](https://www.docs.sui.io/sui-stack/seal), [security considerations](https://www.docs.sui.io/sui-stack/seal/security-best-practices), [provider access](https://www.docs.sui.io/sui-stack/seal/pricing).

## Cases where the stack is not automatically cheaper

At this snapshot's 0.0001 SUI minimum computation component, one million separate transactions require 100 SUI before storage. At the stated reference exchange quote that is $103.40. This is a conditional arithmetic comparison, not a stable dollar tariff.

- CloudTrail data-event delivery is $1 per million events; its first management-event copy is free. One Sui transaction per event therefore does not win on raw event fees here. Batched roots plus blob storage may, but ingestion and search are separate. [CloudTrail pricing](https://aws.amazon.com/cloudtrail/pricing/).
- Step Functions Standard's US East example is $0.000025 per transition, or $25 per million before free allowance. A naive transaction per transition has a higher computation floor here. Keep execution offchain and use selective approvals/settlement if independent verification is valuable. [Step Functions pricing](https://aws.amazon.com/step-functions/pricing/).
- Ordinary IAM is offered without an additional fee; an IAM-like product cannot claim to remove a nonexistent IAM subscription. [IAM FAQ](https://aws.amazon.com/iam/faqs/).
- S3 storage alone, high-volume messaging and commodity queries are not automatically economical onchain. Blob transport, searchable databases and paid compute still exist. Nautilus adds attested offchain execution, not free CPU/GPU capacity. [Nautilus](https://www.docs.sui.io/sui-stack/nautilus).

## Market size, margins and next decision

AWS reports segment finances, not a dependable product-by-product profit ranking. Its FY2025 AWS revenue was about $128.7 billion with $45.6 billion operating income; Q2 2026 was $42.2 billion and $16.6 billion, respectively. Neither establishes which individual service has the highest margin. [FY2025 results](https://ir.aboutamazon.com/news-release/news-release-details/2026/Amazon-com-Announces-Fourth-Quarter-Results/default.aspx), [Q2 2026 results](https://ir.aboutamazon.com/news-release/news-release-details/2026/Amazon-com-Announces-Second-Quarter-Results/default.aspx).

Compute/AI, database, storage, security and operations have different addressable buyers; AWS's total revenue is not our market size. The next decision is to validate SuiFleet's full network bill and demand, then benchmark configuration-history/approval and encrypted configuration distribution using the same cost-first method. Artifact distribution is the closest reusable product extension. Do not start a broad AWS clone or open unrelated implementation scope merely because this document lists hypotheses.

## Manufacturer prospect research

The [50-company discovery shortlist](business/prospects/README.md) supplies a sourced CSV, ranked readable report, explicit migration/safety caveats and prioritized discovery questions (SF-105). It is research only: fleet size, budget and buyer interest remain unknown, and no outreach or pilot has occurred.
