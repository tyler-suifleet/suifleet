# SuiFleet: network-first business plan

Decision basis: 2026-10-16. This records Tyler's original commercial thesis, not a newly discovered positioning idea. Product implementation and hardware acceptance remain separate from business hypotheses.

Updated 2026-10-25: the user selected a subscription floor based on a multiple of SUI-sensitive recurring cost. The [current planning policy](business/SUBSCRIPTION_PRICING.md) uses configurable 3× SUI-cost stress and an 80% infrastructure margin target under stress. Those numerical defaults are planning assumptions; final tariffs and production margins remain unapproved and unmeasured until [SF-135/#267](https://github.com/tyler-suifleet/suifleet/issues/267). The 2026-10-23 prices below are historical commercial candidates, subject to this floor.

## The opportunity

SuiFleet can potentially replace recurring fleet-platform charges with low marginal network usage: publish an authenticated release once, distribute the same artifact across many devices, and pay for authority changes and execution receipts. The opportunity is to pass a substantial share of that infrastructure saving to customers while retaining a margin on measured usage. Cost advantage is part of the product, alongside deployment control and independently verifiable records.

Do not begin with an arbitrary per-device SaaS subscription or use assumed engineering labor to manufacture savings. Establish the network-only bill first. Separately measure any paid delivery, RPC, indexing, hosting and key services needed for the chosen operating model; optional managed hosting and support can be distinct offers. The 2026-10-23 proposal below sets a per-device price from the estimated cost to serve; no production saving percentage or gross margin is established until it is measured on Mainnet.

A fleet with many devices and relatively infrequent releases is the initial economic hypothesis. Idle devices do not themselves require a periodic Sui transaction, although retained objects, active integrations and selected services can have costs. Receipts still scale with device activity: one shared artifact does not make 1,000 device reports one transaction.

## Subscription pricing decision (2026-10-25)

For a fixed, bounded workload, separate monthly SUI-sensitive recurring cost `S`,
WAL expense `W` and other recurring services `O`. Apply the configurable stress
multiple to `S`, then price for the chosen margin under that stress:

```text
risk floor = (3 × S + W + O + fixed payment fee) / (1 − 0.80 − payment fee fraction)
subscription = max(risk floor, monthly minimum, commercial price after discounts)
```

The current planning inputs are 3× SUI-cost stress, 80% stressed infrastructure
margin and provisional payment fees of 3% + $0.30 per invoice. Annual fixed fees
are amortized over 12 months. Volume/annual discounts cannot undercut the floor.
Refundable deposits and gas float remain separate working capital; unknown
required provider costs must be quoted rather than counted as zero. The multiple
covers total SUI-cost exposure, not a separate 3× token shock and 3× gas shock.
WAL stress is independently configurable; its baseline multiplier is 1× under
USD-targeted storage pricing.

The [reproducible policy and calculator](business/SUBSCRIPTION_PRICING.md) show the
formula, source requirements and sensitivity at 1×/2×/3×/5× SUI cost. An explicitly
synthetic $9/month cost split ($8 SUI-sensitive plus $1 other) yields a $148.83
monthly floor with these assumptions, preserving about 80% infrastructure margin
at 3× SUI cost. This is arithmetic, not a measured cost split or a final tariff.
Customer prices remain fixed for purchased terms; a token move does not generate
an automatic usage invoice. Reassess quotes and renewals against current costs.

The historical prices, AWS comparisons, profit and margin tables below were
calculated without this new stress floor. They are not current customer quotes or
margin promises. The dApp price table is still a historical commercial candidate;
final reviewed tariffs must satisfy this policy before being published.

## Pricing and cost model (proposal, 2026-10-23)

Prepared by Claude (claude-opus-5-5) at the user's request on 2026-10-23; the user has not yet approved a final price. Costs are estimates from list prices retrieved that day and from Testnet gas rescaled to Mainnet's 100 MIST reference gas price. No Mainnet transaction has run; [SF-135/#267](https://github.com/tyler-suifleet/suifleet/issues/267) replaces these estimates with a measured bill. Margins are gross margins over infrastructure and network fees; they exclude engineering, support and on-call time.

### Decisions recorded

- **Sui stack only, no servers.** SuiFleet runs on Sui, Walrus, Seal and Sui RPC and operates no application servers: no CDN, indexer server, message broker or hosted worker.
  - The HTTP indexer is being removed from the product path ([SF-137/#282](https://github.com/tyler-suifleet/suifleet/issues/282)).
  - Customers who want data in their own storage integrate it directly ([SF-143/#288](https://github.com/tyler-suifleet/suifleet/issues/288)).
  - Pilots use Mysten's public full node; paying fleets move to a SuiFleet-run Sui full node or a paid RPC plan under SuiFleet's account. Customers never need their own RPC account.
- **Any device data.** Customers can store whatever their devices produce (files, streams, sensor batches, logs), treated as opaque bytes: encrypted on the device, kept on Walrus, and browsed, shared and exported from a Data section in the dApp.
- **All-in per-device price.** Network fees are included. SuiFleet funds each fleet's on-chain budget (SF-083) from the subscription, so customers need not hold SUI or WAL.
- **Configurable cost settings.** Every fleet gets sensible defaults and can change the settings that drive cost, seeing the cost and prepaying it before saving ([SF-130/#265](https://github.com/tyler-suifleet/suifleet/issues/265), [SF-131/#266](https://github.com/tyler-suifleet/suifleet/issues/266)).
- **Packaging by customer segment.** Pricing depends partly on who the customer is, so the same platform is sold in two packages (below), not as two price lists for the same thing.

### Proposed price list

Platform plan, graduated per device per month (each band applies only to the devices in it):

| Devices | Price |
|---|---|
| First 10,000 | $0.08 |
| 10,001 to 100,000 | $0.05 |
| Above 100,000 | $0.04 |
| Above 1,000,000 | Custom, from $0.03 |

| Other terms | Price |
|---|---|
| Monthly minimum | $49 |
| Activation | $0.10 per device, one-time (enrollment objects and gas-float capital) |
| Annual prepay | 10% discount |

The platform price includes the default settings: an update check every 30 minutes (hourly on request), 60-second checks during rollout windows, staged rollouts, daily on-chain health, weekly encrypted log archives kept 6 weeks, enrollment and the dashboard. Live telemetry is not in the base product; it needs an off-chain service and would be a later add-on.

Managed plan: the platform price plus $0.10 per device per month, with a $199 monthly minimum. It adds onboarding, help integrating SuiFleet into the customer's device image, and a support SLA. Its premium funds the support time that gross margin excludes.

Fleet data add-on, for any device data stored on Walrus:

| Item | Price | Our cost at 2026-10-23 rates | Gross margin |
|---|---|---:|---:|
| Included in the platform plan | 10 MB written per device per month, kept for the default 6 weeks (covers default weekly logs) | – | – |
| Data written beyond the allowance | $0.10 per GB | ~$0.018 per GB | ~82% |
| Data stored | $0.05 per GB-month | ~$0.021 per GB-month | ~58% |

For comparison, CloudWatch Logs charges $0.50 per GB ingested plus $0.03 per GB-month, and S3 Standard $0.023 per GB-month plus $0.09 per GB of internet egress. Reading data back from Walrus has no egress fee. Logs count as device data: the log-cadence setting prices how often batches are written, and this add-on prices the volume.

### Customer segments

- **Greengrass switchers** already run fleet tooling and will compare SuiFleet with their AWS bill.
  - Sell the platform plan, quoted side by side with their current bill at matched settings.
  - Aim for at least 50% savings in every profile; the tables below show 56–83%.
  - Offer migration help, for example free months during the switch. Recipe and component translation is on the Greengrass-compatibility roadmap, not a current sales claim.
- **Greenfield customers** have no fleet management today.
  - Their alternatives are building it themselves or turnkey products: Mender at $0.31–0.95, balenaCloud at about $3.08 and Memfault at about $3.50 per device.
  - They value onboarding and support more than the last cent, so sell the managed plan.
  - At 10,000 devices that is $0.18 per device: 42% below Mender Basic and 81% below Mender Professional. Small fleets pay more per device until the $199 minimum is covered.
- Validate both packages and the managed premium with the first customers; neither has been tested with buyers.

### Positioning

- **Infrastructure, not a "blockchain product".** Customer-facing language describes fleet and OTA infrastructure: verifiable release authority, tamper-evident update records, no lock-in and low cost. Sui, Walrus and Seal belong in the technical and security documentation, where security reviews will ask how the infrastructure works.
- **Device data is table stakes.** Every alternative a buyer compares answers "where does my device data go?": Greengrass routes to S3 and Kinesis, Golioth sells pipelines, Memfault centres on diagnostics, Mender sells a monitoring add-on. SuiFleet's answer is the fleet data add-on, plus direct export to the customer's own storage.
- **Deployments, not telemetry pipelines.** AWS provides live telemetry through IoT Core MQTT, device shadows and rules into services such as CloudWatch, IoT SiteWise and Kinesis. SuiFleet targets deployment and fleet management; live telemetry is not a near-term target.
  - Customers who need minute-level telemetry can keep sending it straight to IoT Core or their existing pipeline, without Greengrass, and move deployments to SuiFleet.
  - That still removes Greengrass's $0.16 per-device fee and its Jobs charges.

### Entry offers (proposal)

- **At-cost tier.** Up to 100 devices per organization with no SuiFleet margin: the customer pays only network and hosting costs, about $0.01 per device per month at default settings (about $12 a year for 100 devices).
  - Billed annually in advance, so card fees do not exceed the bill. Customers who prefer to pay in SUI can fund the fleet budget directly; SF-083 already collects network costs up front with no SuiFleet fee.
  - Community support only.
- **Design partners.** Three to five companies from the prospect shortlist.
  - Up to 5,000 devices each at cost for 12 months, with managed onboarding free.
  - In exchange: regular feedback, a production reference, and permission to publish a case study.
  - Afterwards partners move to list price or a pre-agreed discount.

### Configurable settings: added cost, price and margin

| Setting | Option | Added cost, $/device/month | Price, $/device/month | Gross margin |
|---|---|---:|---:|---:|
| Update check | 30 minutes (default) or 1 hour | included | included | – |
| Update check | 15 minutes | 0.0036 | 0.01 | 64% |
| Update check | 5 minutes | 0.018 | 0.05 | 64% |
| On-chain health | Daily (default) | included | included | – |
| On-chain health | Every 6 hours | 0.009 | 0.03 | 70% |
| On-chain health | Hourly | 0.069 | 0.12 | 42% |
| Log archives | Weekly, kept 6 weeks (default) | included | included | – |
| Log archives | Daily | 0.028 | 0.08 | 65% |
| Log retention | 6 months | 0.006 | 0.02 | 70% |
| Log retention | 1 year | 0.014 | 0.04 | 65% |
| Encrypted delivery (Seal) | On | ~0.005 | 0.02 | 75% |

Update-check costs assume paid RPC at about $2.50 per million requests. On the public endpoint or SuiFleet's own node their marginal cost is near zero. Hourly health is deliberately priced at a lower margin so heavy fleets still save about 58–60% against Greengrass.

### Cost to serve, margin and Greengrass comparison by profile

Each Greengrass usage profile is matched with the SuiFleet settings that cover the same workload:

| Profile | Greengrass workload | SuiFleet equivalent settings | SuiFleet platform price, $/device/month (first 10,000 devices) |
|---|---|---|---:|
| Light | One 50 MB deployment a month, status every 5 minutes, no cloud logs | 30-minute checks, daily on-chain health, logs off | 0.08 |
| Typical | Four 50 MB deployments, telemetry each minute, hourly shadows, 10 MB of logs | Defaults: 30-minute checks, daily health, weekly logs kept 6 weeks | 0.08 |
| Heavy | Eight 200 MB deployments, telemetry every 10 s, 100 MB of logs, tunnels, Device Defender | 15-minute checks (+0.01), hourly health (+0.12), daily logs kept 6 weeks (+0.08), 90 MB of data beyond the allowance (+~0.015) | ~0.305 |

Monthly totals for the whole fleet on the platform plan:

| Devices | Profile | SuiFleet cost to serve | SuiFleet list revenue | Gross margin | Greengrass | Customer saving vs Greengrass |
|---:|---|---:|---:|---:|---:|---:|
| 1,000 | Light | ~$3 | $80 | ~96% | $182 | 56% |
| 1,000 | Typical | ~$9 | $80 | ~89% | $259 | 69% |
| 1,000 | Heavy | $108–157 | $305 | 48–65% | $766 | 60% |
| 10,000 | Light | ~$83 | $800 | ~90% | $1,822 | 56% |
| 10,000 | Typical | ~$138 | $800 | ~83% | $2,590 | 69% |
| 10,000 | Heavy | $1,340–1,500 | $3,050 | 51–56% | ~$7,300 | 58% |
| 100,000 | Light | $920–1,600 | $5,300 | 70–83% | ~$18,200 | 71% |
| 100,000 | Typical | $1,470–2,150 | $5,300 | 59–72% | ~$25,200 | 79% |
| 100,000 | Heavy | $11,600–12,300 | $27,800 | 56–58% | ~$67,000 | 58% |
| 1,000,000 | Light | $4,700–5,900 | $41,300 | 86–89% | ~$180,000 | 77% |
| 1,000,000 | Typical | $10,200–11,500 | $41,300 | 72–75% | ~$243,000 | 83% |
| 1,000,000 | Heavy | $111,100–113,700 | $266,500 | 57–58% | ~$629,000 | 58% |

Notes on the comparison:

- **Heavy is dominated by hourly on-chain health.** Hourly health costs SuiFleet about $0.072 per device and is priced at $0.12. If a heavy fleet keeps daily health instead, it pays SuiFleet about $0.185 per device for its first 10,000 devices, 76% less than Greengrass heavy's $0.77. SuiFleet's cost for that fleet falls to about $0.04–0.05 per device: $0.038 in network fees plus about $0.009 of RPC on a paid plan.
- **Gross margin** means (revenue − cost to serve) ÷ revenue. For example, typical at 1,000 devices is ($80 − $9) ÷ $80 = 89%, which is a markup of about 790% on cost.
- **Gross profit** on the typical profile is about $71 a month at 1,000 devices, $660 at 10,000, $3,100–3,800 at 100,000 and $30,000 at 1,000,000. That is before engineering, support, on-call and card-processing fees (about 3% plus $0.30 per invoice).
- **The equivalents are not feature-identical.** SuiFleet replaces Greengrass's live telemetry with signed on-chain health at the chosen cadence, and its rows include no cost for secure tunnels or Device Defender.
- **Greengrass totals use list prices,** including IoT Core's lower messaging tiers above 1 billion messages and S3's lower egress tiers. Greengrass's own $0.16 per device has no published volume discount.
- **Ranges** cover RPC on the public endpoint versus a paid plan or one to several SuiFleet nodes. Their upper ends also include an optional SuiFleet Walrus aggregator allowance, a conservative assumption that the plan below avoids.

Cost components per device per month:

- On-chain health: $0.003 daily, $0.072 hourly.
- Log archives: $0.004 weekly, $0.032 daily for 100 MB a month. Both require deposit reclaim; the weekly figure is about $0.10 before [SF-129/#264](https://github.com/tyler-suifleet/suifleet/issues/264).
- Deployment receipts: about $0.00035 each (four a month: $0.0014).
- RPC:
  - $0 at 1,000 devices, on the public endpoint or Chainstack's free tier.
  - $49–199 a month on a paid plan at 10,000 devices.
  - SuiFleet nodes from about 100,000 devices: $520–1,040 a month at 100,000, and $1,040–4,160 at 1,000,000.
- Artifact downloads: free from public Walrus aggregators, with a fallback list and ranged downloads (SF-124). Direct reads from storage nodes are not needed now and are deferred ([SF-144/#290](https://github.com/tyler-suifleet/suifleet/issues/290), closed as not planned unless public aggregators prove inadequate). The tables keep a $60–1,300 a month allowance for an optional SuiFleet aggregator at large scale as a conservative assumption.

Owner-side release uploads add about $0.03 per 50 MB release per fleet. One-time per device: about $0.03 of mostly refundable storage deposits plus a 0.06 SUI gas float, which is working capital (about 60,000 SUI at 1,000,000 devices).

Reference values on 2026-10-23:

- SUI $0.987 and WAL $0.0368 (CoinGecko).
- Sui reference gas price 100 MIST, storage price 76, storage rebate 99.99%.
- Walrus 57,374 FROST per encoded MiB per epoch, and 104,294 per encoded MiB written.

### Comparison baseline

Greengrass all-in at 1,000 devices costs $0.18 (light), $0.26 (typical) and $0.77 (heavy) per device per month. These use us-east-1 list prices from the AWS Price List API on 2026-10-23, with no free tier or discount. [Greengrass](https://aws.amazon.com/greengrass/pricing/) publishes no volume discount; see also [IoT Core](https://aws.amazon.com/iot-core/pricing/) and [Device Management](https://aws.amazon.com/iot-device-management/pricing/).

Turnkey OTA products at about 1,000 devices, per device per month:

| Product | Price |
|---|---|
| [Mender](https://mender.io/pricing) Basic | $0.31 |
| Mender Professional | $0.95 |
| [Qbee](https://qbee.io/pricing/) | ~€0.88 |
| [Particle](https://www.particle.io/pricing/) | $2.99 |
| [balenaCloud](https://www.balena.io/pricing) | $3.08–3.22 |
| [Memfault](https://memfault.com/pricing/) | $3.50 |

Azure IoT Hub with Device Update costs $0.17–0.64 depending on the profile. With equivalent settings, the platform plan costs customers 56–77% less than Greengrass for light fleets, 69–83% less for typical, and 58–60% less for heavy (75–77% less with daily health). The $0.08 small-fleet platform price is 74% below Mender Basic.

### Why today's build costs more

As built, every daemon polls every 10 seconds with about 23 gRPC reads, or about 45 with on-chain health and archives on. Per 1,000 devices that is about 11.7 billion reads and 39 TB of RPC traffic a month, which needs two dedicated full nodes. A 1,000-device fleet therefore costs about $0.78–2.29 per device per month, 3–9 times Greengrass, even though its network fees are under $4 a month. One device alone exceeds Mysten's documented public-endpoint guidance of 100 requests per 30 seconds.

### RPC and hosting path

- **Mysten public full node** ([best practices](https://docs.sui.io/references/sui-api/rpc-best-practices)), used for pilots:
  - Free, with documented guidance of 100 requests per 30 seconds. A 180-request probe on 2026-10-23 was not throttled.
  - Keeps about 14 days of event history.
  - Documented for development and testing.
- **Chainstack:**
  - Probed live on its free tier on 2026-10-23. It serves every call the daemon uses, including indexed `ListEvents` back about 7.7 months.
  - Free tier: 3M requests a month. Paid plans: about $2.50 per million requests ([guide](https://chainstack.com/how-to-get-sui-rpc-endpoint-2026/)).
  - Its [terms](https://chainstack.com/tos/) appear to allow serving SuiFleet's devices from SuiFleet's account, since end-user traffic is normal use; confirm in writing before relying on it.
  - A token on devices can be extracted, so rely on plan rate caps, alerts and token rotation.
- **SuiFleet Sui full node:**
  - Hetzner AX102-class bare metal with extra NVMe, about €449 a month.
  - Cheaper than paid RPC above about 200M requests a month, roughly 100,000 devices at 30-minute checks.
  - Needs prompt upgrades and monitoring.
- **Network nodes versus servers.** At large scale SuiFleet would run Sui full nodes to absorb read volume. Walrus aggregators are not needed: devices use public aggregators. Direct storage-node reads (SF-144) are deferred and hopefully never needed; a SuiFleet aggregator would only ever be an optional cache, never something devices depend on.

### Development required

Stage 1, pilot-ready on the public endpoint (no contract change):

- [SF-122/#254](https://github.com/tyler-suifleet/suifleet/issues/254): one batched chain read per poll.
- [SF-123/#262](https://github.com/tyler-suifleet/suifleet/issues/262): 30-minute checks, rollout fast-checks, jittered backoff, and RPC endpoint fallback.
- [SF-124/#255](https://github.com/tyler-suifleet/suifleet/issues/255): public Walrus aggregators with fallback and ranged downloads.
- [SF-126/#256](https://github.com/tyler-suifleet/suifleet/issues/256): the dApp and indexer stop full-history scans and per-device sweeps.

Stage 2, robust on public endpoints (additive Move upgrade):

- [SF-125/#263](https://github.com/tyler-suifleet/suifleet/issues/263): device inbox, group heads and per-member index; scheduled starts on time.
- [SF-130/#265](https://github.com/tyler-suifleet/suifleet/issues/265): on-chain per-fleet cost settings.
- [SF-131/#266](https://github.com/tyler-suifleet/suifleet/issues/266): settings page with live cost and a prepaid deposit.

Stage 3, scale and the cost floor:

- [SF-127/#257](https://github.com/tyler-suifleet/suifleet/issues/257): sharded receipts.
- [SF-128/#258](https://github.com/tyler-suifleet/suifleet/issues/258): protected-blob cap.
- [SF-129/#264](https://github.com/tyler-suifleet/suifleet/issues/264): deposit reclaim.
- [SF-121/#252](https://github.com/tyler-suifleet/suifleet/issues/252): device-local archive quilting.
- [SF-132/#259](https://github.com/tyler-suifleet/suifleet/issues/259): batched gas-account enrollment.
- [SF-133/#260](https://github.com/tyler-suifleet/suifleet/issues/260): threshold Seal key fetch.

Stage 4, no servers and fleet data:

- [SF-137/#282](https://github.com/tyler-suifleet/suifleet/issues/282): remove the server indexer from the product path.
- [SF-138/#283](https://github.com/tyler-suifleet/suifleet/issues/283): gate staged rollouts on signed on-chain health instead of server telemetry.
- [SF-139/#284](https://github.com/tyler-suifleet/suifleet/issues/284): store any device data on Walrus.
- [SF-140/#285](https://github.com/tyler-suifleet/suifleet/issues/285): Data section in the dApp for browsing, downloading, sharing and exporting.
- [SF-141/#286](https://github.com/tyler-suifleet/suifleet/issues/286): per-stream retention and expiry.
- [SF-142/#287](https://github.com/tyler-suifleet/suifleet/issues/287): meter and price device data.
- [SF-143/#288](https://github.com/tyler-suifleet/suifleet/issues/288): export directly to customer-owned storage.

Bug: [SF-134/#261](https://github.com/tyler-suifleet/suifleet/issues/261), where the indexer shows foreign deployments in a device's history.

Validation: [SF-135/#267](https://github.com/tyler-suifleet/suifleet/issues/267), a measured Mainnet bill, after the SF-087 Mainnet deployment.

### Risks

- **Token and fee exposure.** Use the [stressed subscription floor](business/SUBSCRIPTION_PRICING.md), not a fixed margin deduction extrapolated from the historical price table. SUI-sensitive recurring costs are stressed independently of WAL/USD-service expense. Walrus targets USD storage prices with periodic adjustments, not instantaneous repricing; test an independent WAL shock where needed. A shock beyond the selected assumptions can still compress margins. Refundable deposits/gas float also create capital exposure, separately from recurring expense.
- **No Mainnet measurement yet** (SF-135).
- **The public endpoint is guidance-limited** and not intended for production; move paying fleets off it before relying on it.
- **The weekly-archive figure** depends on deposit reclaim (SF-129) and on SF-121's archive work.
- **The Greengrass comparison** is modelled list pricing; a customer's actual AWS bill may differ.

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

Use an auditable usage model: network fees + separately metered selected infrastructure + an explicit commercial markup. The 2026-10-23 proposal expresses the markup as an all-in per-device price with priced add-ons for each cost setting; revisit it once SF-135 measures complete bills. Exclude labor from the base cost comparison; price requested integration/support separately. Do not assume percentage markup on cents will fund a company: validate fleet scale, paid usage and optional service demand.

Before quoting a production fleet, measure a representative release end to end; count actual transactions/bytes and successful/failed effects; capture storage deposits/refunds, renewal and upload fees; verify delivery at expected concurrency; account for selected service charges; and compare the same workload, retention, security and availability against the customer's actual AWS configuration. Test Mainnet simulation/estimation without publication where possible; live Mainnet spend still requires explicit user authorization. Validate purchasing demand alongside the technical prototype.

**Platform subscription decision (2026-10-23).** The user selected fixed platform subscriptions with device-capacity tiers and purchased reporting settings, rather than usage invoices. New-plan defaults are daily status and disabled log uploads; enabling logs selects an upload interval, bounded bytes, retention and a diagnostic/crash allowance. Show the price before plan changes; do not generate automatic overage invoices. The $100/month for 1,000 devices example is a modeling scenario, not an approved price or demonstrated margin. Payment method, final prices and subscription collection/enforcement remain unimplemented decisions. **Current prepaid network costs (SF-083):** the fleet budget still collects network funds only and charges no SuiFleet service fee. Customer deposits are not subscription revenue. Sponsored transactions through a gas station, and Nautilus-enclave sponsorship, are recorded as future options, not the current design: both need an always-on signer or self-managed enclave hosting. See [FLEET_BUDGET.md](FLEET_BUDGET.md). Fleet settings funding is specified separately in [SF-131/#266](https://github.com/tyler-suifleet/suifleet/issues/266).

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

SF-142 implements the proposed data rates and allowance in the shared configurable
price table and Settings planner, with separate network cost/deposit estimates.
The Data page reconciles current on-chain records and lifetime encoded-write
counters per uploading-device stream; it does not charge customers or claim a
complete historical billing ledger after records are released. See
[the metering definitions and pricing assumptions](FLEET_BUDGET.md#device-data-metering-and-price-proposal-sf-142).
