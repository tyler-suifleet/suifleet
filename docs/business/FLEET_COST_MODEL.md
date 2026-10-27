# Encrypted fleet network cost model (SF-054)

This is a reproducible accounting model and a set of network observations, **not
a complete production bill**. Unknown production services remain unquoted, rather
than being priced at zero. Neither the Mainnet subtotal nor the historical Testnet
cashflows establish a customer-ready price or a numerical AWS break-even point.

Observed 2026-10-21 by Codex, configured/running model `gpt-6.1-sol`, on branch
`work/suifleet/sf-054-18c9ec41`. [SF-054/#97](https://github.com/tyler-suifleet/suifleet/issues/97)
depends on the merged [SF-022/#25](https://github.com/tyler-suifleet/suifleet/issues/25).
Production provider/credential and manual wallet acceptance is deferred under the
user's implementation/acceptance split to [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
That deferral does not turn an unquoted charge into a measured fee.

## Evidence and workload

The [network snapshot](sf054/network-quotes.json) records Sui Mainnet epoch 1287,
protocol 138, reference gas price 100 MIST; Sui Testnet epoch 1259, protocol 139;
and Walrus Mainnet epoch 42 with 1,000 shards. Raw Mainnet rates were 57,321 FROST
per encoded MiB per epoch and 104,135 FROST per encoded MiB written. The
[independent check](sf054/independent-mainnet-rates.json) reads the Mainnet object
through gRPC and the official SDK, separately from the GraphQL reader, and agrees.
Both are read-only. No Mainnet transaction was simulated, signed or submitted.

The same snapshot records CoinGecko market quotes, including their update times:
SUI $1.048 and WAL $0.04023726. These indicative spot conversions exclude exchange
spread, execution slippage, taxes and token acquisition fees; they are not provider
quotes or guaranteed settlement prices. Never convert valueless Testnet tokens
into a Mainnet USD invoice.

The [offline encryption specimen](sf054/encrypted-specimen.json) uses the actual
app `sealer` and Seal 1.5.0 AES-GCM encoding, a 2-of-3 threshold, and test-vector
public keys. The recipe passes the app schema validator and uses the app's
`JSON.stringify(recipe, null, 2)` byte representation. Content is synthetic and
was not deployed to a device; test-vector identities confer no production access.

| Quantity | Artifact | Recipe |
|---|---:|---:|
| Plaintext bytes | 1,048,576 | 317 |
| App envelope bytes uploaded | 1,049,122 | 860 |
| Exact RS2 encoded bytes | 70,038,000 | 66,034,000 |
| Billable encoded MiB, rounded up | 67 | 63 |
| Mainnet gross storage, 2 epochs (FROST) | 7,681,014 | 7,222,446 |
| Mainnet gross write (FROST) | 6,977,045 | 6,560,505 |
| Mainnet gross subtotal (WAL) | 0.014658059 | 0.013782951 |

These sizes come from the pinned audit SDK's exact RS2 codec calculation, **not**
`4.5 × plaintext + 64 MB`. The native Testnet publisher independently returned the
same encoded sizes. Encryption is randomized: reruns have different ciphertext
hashes but the same sizes for this specimen. Frozen hashes identify the particular
uploaded bytes; the code regenerates equivalent size specimens without preserving
secret material. Current daemon plaintext bounds are 128 MiB by default, so the
older 500 MB example is not silently promoted into a supported default workload.

The [scenario input](sf054/scenario.json) and [result](sf054/scenario-result.json)
specify 1,000 devices, four shared releases, one artifact and one recipe per release,
one **device-target deployment per device per release**, one final report each,
zero retries and zero renewals. There are eight shared blob uploads, 4,000 deployment
objects and 4,000 report transactions. Reusing shared artifact bytes does not remove
per-device deployment/receipt costs. A single group-target deployment is a different
workload: membership, policy grants, larger receipt fields, contention and PTB
batching need separate measurements. We do not extrapolate its gas from one receipt.

## Actual Testnet storage and effects

Two requests used the public Testnet publisher's real HTTP upload path with
`epochs=2&send_object_to=<dedicated agent address>`. The global
`network:suifleet-testnet` lock was held during writes and then released; coordination
was posted in [#11](https://github.com/tyler-suifleet/suifleet/issues/11#issuecomment-6322366170).
The wallet config was passed explicitly to the pinned transaction CLI for an
active-address check. Actual chain `4c78adac` was verified. No agent-wallet
transaction, faucet request, funding transfer, publication or upgrade was needed.
The public publisher sponsored the SUI/WAL. This sponsorship is a Testnet service
observation, not a zero-priced production offering or an unlimited delivery SLA.

The executed upload commands, after the lock and account/chain checks, were:

```bash
curl --silent --show-error --max-time 55 -D /tmp/sf054-artifact-headers --output /tmp/sf054-artifact-publisher.json --request PUT --header 'Content-Type: application/octet-stream' --data-binary @/tmp/sf054-blobs/artifact.seal 'https://publisher.walrus-testnet.walrus.space/v1/blobs?epochs=2&send_object_to=0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965'
curl --silent --show-error --max-time 55 -D /tmp/sf054-recipe-headers --output /tmp/sf054-recipe-publisher.json --request PUT --header 'Content-Type: application/octet-stream' --data-binary @/tmp/sf054-blobs/recipe.seal 'https://publisher.walrus-testnet.walrus.space/v1/blobs?epochs=2&send_object_to=0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965'
```

Both returned HTTP 200, then independent chain reads verified effects and owners.
These are recorded mutations, not commands in the offline reproduction suite.

[The storage evidence](sf054/walrus-testnet.json) contains publisher responses,
independently reread object contents, owners, complete object histories, transaction
commands, effects and balance changes. Each blob used one PTB containing
`reserve_space` + `register_blob`, followed by one `certify_blob` PTB and object
transfer. No unrelated blob operations were bundled into these four transactions.

| Actual operation | Net SUI cashflow (MIST) | WAL spent (FROST) |
|---|---:|---:|
| Artifact reserve/register | 2,987,376 | 699,480 |
| Artifact certify/transfer | 1,030,696 | 0 |
| Recipe reserve/register | 2,987,376 | 657,720 |
| Recipe certify/transfer | 1,030,696 | 0 |
| Sponsored Testnet total | 8,036,144 | 1,357,200 |

SUI balance deltas equal minus net gas, and WAL deltas equal minus the publisher's
reported purchase cost, with zero residuals for both tokens. Both Blob objects are
owned by the dedicated agent, certified in Walrus Testnet epoch 557 and store the
actual interval `[557,559)`. The initial HTTP responses had `certifiedEpoch: null`;
onchain rereads, not that response alone, establish certification. Each object's
recorded storage rebate is 2,006,400 MIST. These two objects are retained as short
lived evidence; no deletion, renewal or rebate from them is claimed. WAL storage
payments are consumed purchases, not refundable SUI deposits.

Two Testnet epochs are nominally two days, not 28 days. On Mainnet two epochs are
nominally 28 days; an upload partway through an epoch does not establish 28 full
days remaining. Use the actual reservation start/end epochs, chain time and the
publisher/client's purchase rules for a required wall-clock deadline. Rollover
and deduplication may change the interval; an already-certified blob can be owned
by someone else and cannot be assumed renewable. The Mainnet rate arithmetic above
does not assert a purchased Mainnet expiry or subsidy.

Historical SF-022 deployment effects were also reread, rather than inferred from
funding or budgets. They concern a **different encrypted delivery run with a local
Walrus stand-in**, not an end-to-end deployment of the specimens above.

| Historical one-device operation | Computation | Storage charged | Storage rebate | Net MIST |
|---|---:|---:|---:|---:|
| Create deployment | 1,000,000 | 12,502,000 | 0 | 13,502,000 |
| Applied receipt | 1,000,000 | 7,926,800 | 7,675,232 | 1,251,568 |
| Delete deployment | 1,000,000 | 0 | 12,751,525 | -11,751,525 |
| Complete object lifetime | | | | 3,002,043 |

The complete digests and 21 historical effects are in the network snapshot.
Creation `Gr1L1HSd…`, report `5sSF3Gov…`, deletion `8TzCZE7j…` belong to the same
historical cycle. Net SUI cashflow is `computationCost + storageCost - storageRebate`.
`nonRefundableStorageFee` is already represented in that accounting and must not be
added again. A spending allowance clamps negative net gas conservatively; an
economic ledger preserves the signed cleanup credit. Historical policy creation
cost 4,245,200 MIST and its device grant cost 1,243,525 MIST. Historical registration
and activation effects use an older-package migration fixture and do not measure
current-package onboarding.

## Complete accounting boundary

The model keeps every category explicit. `unquoted` and `excluded` amounts are null;
omission cannot create a complete bill. Net gas cashflow includes object deposits;
permanent consumed fees require a complete lifetime with actual cleanup. Gas
budgets and required funding balances are exposure/liquidity, not consumed fees.
Internal operator-to-device funding cancels when consolidating fleet balances.
Do not add it a second time to device report gas.

| Category | Evidence / treatment |
|---|---|
| Registration, enrollment, identity rotation and groups | Historical effects retained; current deployment workload unquoted |
| Policy creation, grants and revocation/rotation | Historical examples; exact production scope/batching unquoted |
| Deployment/target objects and all reports | Historical exact effects; extrapolation explicitly Testnet and one device target |
| Walrus artifact **and recipe** reserve/register/certify | Four current Testnet effects and WAL deltas; Mainnet gross WAL rates independently checked; Mainnet gas/deposits unquoted |
| Renewal | Base scenario has none. Add exact extension epochs, remaining capacity, WAL/subsidy quote, owner authority and extension SUI effects for longer retention |
| Cleanup/rebates | Historical deployment deletion measured. SF-129 measured expired-archive and finished-record reclaim on an isolated chain ([storage reclaim](../STORAGE_RECLAIM.md)); a successful Testnet archive reclaim awaits expiry. No assumed WAL refund |
| Failed execution/retries | Zero in this observed upload run. Failed effects still charge gas; count each attempt and retain its digest/status |
| Unknown execution | Never report zero or retry blindly. Reserve full gas budget plus unresolved WAL/payment exposure until effects reconcile; keep the bill incomplete |
| Seal key servers | Production independent providers, authentication, fees, per-session/key quotas, persistence and recovery unselected/unquoted; no production committee support inferred from test servers |
| Publisher/relay | App uses an HTTP publisher; production publisher quote unquoted. Relay is an alternative path, not an extra mandatory charge |
| Aggregator/gateway delivery | Bandwidth, cache misses, device downloads/retries, geographic availability and SLA unquoted |
| RPC and indexer | Auth, reads, rate limits, history retention and availability unquoted; no unlimited free production claim |
| Labor | Excluded from network base case; quote hours/rates separately if requested |
| Margin, arbitrary subscriptions and financing | No invented allocation; only include an actually selected service quote |

The relay's read-only Mainnet tip schedule is 40 MIST per **rounded-up encoded
KiB**, base zero. For these specimens that would be 2,735,880 and 2,579,480 MIST
per upload. It is not charged in the HTTP-publisher model, and it does not include
Sui transaction gas or all delivery services.

## Raw WAL versus advertised USD storage

[Walrus storage documentation](https://docs.wal.app/docs/system-overview/storage-costs)
advertises approximately $0.023 per **encoded** GB-month and describes periodic
onchain price updates. Its [tokenomics documentation](https://docs.wal.app/docs/system-overview/wal-tokenomics-faq)
also describes optional storage subsidies and a separate write fee. Do not apply
$0.023 to plaintext GB, substitute it for an actual WAL purchase, or confuse it
with the one-off write charge or SUI gas.

At this observation, using decimal GB, a 30-day comparison month and a 14-day
Mainnet epoch, the raw storage-only rate converts to:

`57321 FROST/MiB/epoch ÷ 1e9 × (1e9/1048576) × (30/14) × $0.04023726/WAL`

`= $0.004713412653636932373046875 per encoded GB per 30 days`.

That is about 4.87969 times below the advertised approximation. Equating those
rates would require a WAL conversion of about $0.19634542, rather than this
snapshot's $0.04023726. MiB/decimal-GB conversion and 28/30-day alignment do not
explain that difference. A positive subsidy would reduce the raw payment further,
so it cannot by itself explain the higher advertised approximation. Price-vote
timing, actual subsidy selection and publisher commercial terms require an actual
current purchase quote; no cause or fiat peg is established by these reads.

For one specimen pair, raw storage-only conversion is $0.0005996743949196, versus
$0.0029210122666667 from the advertised approximation over the nominal 28-day
interval. Raw **storage plus writes** is 0.028441010 WAL, about $0.0011443883140326.
Those are different scopes. We retain the discrepancy explicitly instead of
silently choosing the smaller number as a promised production price. The older
[2026-10-16 snapshot](network-costs-2026-10-16.json) remains historical approximate
arithmetic and is not overwritten or recast as this measurement.

## Per-release/device scenarios and AWS comparison

| Scenario component (1,000 devices, 4 releases) | Fleet interval | Per release | Per device over interval |
|---|---:|---:|---:|
| Mainnet gross artifact + recipe WAL rate estimate | 0.113764040 WAL | 0.028441010 WAL | 0.000113764040 WAL |
| Spot USD conversion of that WAL subtotal | $0.0045775532561304 | $0.0011443883140326 | $0.0000045775532561304 |
| Historical Testnet object lifetime + sponsored storage extrapolation | 12.040316576 test SUI | 3.010079144 test SUI | 0.012040316576 test SUI |
| Complete production network bill | **Unquoted** | **Unquoted** | **Unquoted** |

The Testnet extrapolation assumes independent one-target lifetimes with immediate
deployment deletion. It is not a Mainnet gas prediction, and does not include
current onboarding, policy operations, retained receipt history or production
services. A receipt retained in the live deployment object for 28 days is a
different cashflow/deposit schedule. Historical availability after deletion needs
an indexer/archive quote; deletion does not establish free permanent receipt reads.

[AWS Greengrass pricing](https://aws.amazon.com/greengrass/pricing/) lists $0.16
per active Core device-month, with other AWS services charged separately.
For 1,000 active cores in one calendar billing month that component is $160,
or $40 allocated per four releases, $0.16 per device. A nominal 28-day Walrus
reservation is not automatically an AWS calendar billing month. Free-tier and
cross-service allowances are account-dependent and are not assumed here.

For an explicitly **illustrative US-region S3 Standard marginal-rate comparison**,
use the same four 1 MiB plaintext artifacts plus 317-byte recipes, 28-day retention,
eight PUTs, 8,000 GETs, and 4,000 device downloads of each artifact/recipe pair:

| AWS component | Illustrative charge | Scope still needed for a matched quote |
|---|---:|---|
| Active Greengrass cores | $160 | Calendar billing month; same active-device count |
| S3 Standard storage, $0.023/GB-month | $0.0000900649456 | 30-day normalization here, one regional copy |
| PUTs, $0.005/1,000 | $0.000040 | No additional multipart/retry requests |
| GETs, $0.0004/1,000 | $0.003200 | No device redownloads/cache misses |
| Internet egress at an assumed $0.09/GB marginal tier | $0.37760148 | Region/tier/allowances unquoted; assumption, not a verified customer rate |
| IoT connectivity/messages/jobs, credentials/KMS, logs/history, replication and support | Unquoted | Select actual services; do not automatically add a separate Jobs fee to a Greengrass deployment |

S3 request/storage reference rates are corroborated by the current
[AWS cost guide](https://docs.aws.amazon.com/solutions/latest/automated-security-response-on-aws/cost.html);
obtain a region/account-specific [AWS estimate](https://calculator.aws/) before a
purchase comparison. S3 regional redundancy/availability and any optional replication
must be matched to the selected Walrus delivery/provider availability. We have no
selected production Walrus gateway/key-provider SLA, so the table is not an
availability-equivalent bill and cannot substantiate a savings claim.

For matched retention, device downloads, failure rates, geography, availability,
key management and receipt history, let fleet cost be `F + N*v` and AWS cost
`A + N*a`. The device-count break-even is `(F-A)/(a-v)` when `a != v`, subject to
the applicable pricing tiers. `break_even(F-A, v, a)` implements the case with
nonnegative net fixed cost; AWS fixed costs can instead be moved to the fleet side
only when that difference is nonnegative. Both complete quotes are required.
For the simple hypothetical `A=0, F=$100, v=$0.06, a=$0.16`, equality is at 1,000
devices; those are **test inputs**, not SuiFleet prices. With unknown production
costs, the real break-even is **unquoted**, not zero devices or "always cheaper".

## Reproduction and validation

Offline model and fixture arithmetic, with no credentials or remote mutation:

```bash
python3 -m unittest discover -s scripts/tests -p test_fleet_costs.py -v
python3 scripts/build_fleet_cost_model.py --devices 1000 --releases 4 > /tmp/sf054-model.json
python3 scripts/fleet_costs.py /tmp/sf054-model.json
python3 scripts/fleet_costs.py /tmp/sf054-model.json --require-complete
# Last command intentionally exits 2: the example is not a complete invoice.
```

Read-only refresh (network failure or missing historical data fails visibly):

```bash
python3 scripts/capture_cost_quotes.py > /tmp/sf054-quotes.json
```

For an independent read-only Mainnet SDK check, install audit tools separately
from the app. The audit uses Sui SDK 2.37.0 because Walrus 1.2.36 requires it;
**the app remains on its locked Sui SDK 2.36.0**. This is not an application SDK
upgrade. No signing key or wallet is accepted by these tools.

```bash
npm install --prefix /tmp/sf054-live-sdk --legacy-peer-deps --ignore-scripts --no-audit --no-fund --save-exact @mysten/walrus@1.2.36 @mysten/sui@2.37.0
node scripts/check_mainnet_cost_rates.mjs /tmp/sf054-live-sdk /tmp/sf054-quotes.json
node scripts/test_cost_codec.mjs /tmp/sf054-live-sdk
```

With the app's locked dependencies installed, generate new offline envelopes:

```bash
node scripts/cost_workload.cjs /tmp/sf054-blobs > /tmp/sf054-workload.json
node scripts/quote_blob_cost.mjs /tmp/sf054-live-sdk /tmp/sf054-quotes.json /tmp/sf054-blobs/artifact.seal 2
node scripts/quote_blob_cost.mjs /tmp/sf054-live-sdk /tmp/sf054-quotes.json /tmp/sf054-blobs/recipe.seal 2
```

To independently reread the frozen Testnet storage purchases without uploading:

```bash
python3 -c 'import json,pathlib; d=json.load(open("docs/business/sf054/walrus-testnet.json")); [pathlib.Path("/tmp/sf054-"+u["kind"]+"-response.json").write_text(json.dumps(u["publisher_response"])) for u in d["uploads"]]'
python3 scripts/capture_walrus_effects.py /tmp/sf054-artifact-response.json /tmp/sf054-recipe-response.json
```

These commands do not upload or spend. Future **new** Testnet uploads need the
standing authorization, chain/account preflight, shared resource lock and allowance
rules in [TESTNET.md](../TESTNET.md). Do not blindly repeat a timed-out mutation.
Mainnet spending and paid purchases remain unauthorized.

Current scoped validation: 13 offline accounting tests pass; the pinned SDK matches
both independently measured native-publisher encoded sizes and MiB rounding
boundaries; GraphQL and gRPC/SDK Mainnet rates agree; four live Testnet publisher
transactions succeeded with exact SUI/WAL reconciliation. No production service,
Mainnet write, physical hardware, actual extension wallet, renewal, blob deletion,
or new full encrypted device end-to-end run is claimed. Existing SF-022 encrypted
device execution evidence remains separately linked. Independent review and
current-head CI/testnet checks remain required for the implementation PR.

## Follow-up archive economics

The [SF-121 controlled experiment](SF121_ARCHIVE_COSTS.md) separates recurring
fees, refundable storage capital, equal retention and owner cleanup. It does not
replace the distinct release-delivery workload above or establish a full monthly
production bill.

## Subscription pricing from cost exposure (2026-10-25)

The [SUI cost sensitivity analysis](SUBSCRIPTION_PRICING.md) separates SUI-sensitive
expense, WAL expense, USD/service expense and SUI-denominated working capital, and
reports margin, breakeven and price floors as the USD cost of SUI operations moves
between 0.25× and 20× of the quote basis. Its policy is 50% margin at 3× and
breakeven at 10× without a hedge, a term hedge bought at invoice (which covers
token-price but not gas-price moves), and renewal
repricing from 3×. Its examples use the business plan's typical-profile estimates,
not this historical, incomplete SF-054 scenario, and they do not establish a
measured production bill. Supply missing costs before pricing; never use Testnet
balances or assumed refunds as recurring revenue.

## Resumable observation preparation (SF-155)

Use the [collector and two-week procedure](FLEET_COST_COLLECTION.md) to preserve
actual effects, WAL retention, request/provider observations, capital and balance
reconciliation. Its export uses this accounting schema. Missing services,
unresolved outcomes and interval coverage remain incomplete; final bill acceptance
stays in SF-135/#267 and production acceptance in SF-087/#159.
