# Subscription pricing under SUI cost changes

Planning policy proposed 2026-10-25; not an approved customer tariff, treasury
purchase or contract term. SuiFleet sells fixed USD subscriptions for a bounded
workload (device capacity plus purchased check, health, log and data settings),
with no automatic usage-overage invoices. Part of the cost of serving that
workload is paid in SUI, so a move in the USD cost of SUI operations changes
SuiFleet's margin, not the customer's price, until the term renews. This document
and the [calculator](../../scripts/subscription_pricing.py) work through what
happens to cost, margin and revenue as that cost moves between 0.25× and 20× of
the quote basis, how much of a move each proposed price can absorb, and the three
controls SuiFleet uses: price floors, a term hedge and renewal repricing.

The [planning policy](subscription-policy.json) holds the current controls:

- **Floors.** A price must keep at least **50% infrastructure margin at 3× SUI cost**
  and must **not lose money at 10× SUI cost** (the user's requested boundary),
  measured without any hedge. A list price above both floors is unchanged by them.
- **Term hedge (proposed).** When an invoice is paid, SuiFleet would buy the SUI the
  term's SUI-sensitive operations will consume (`term_sui_purchased_fraction` = 1).
  Floors may be evaluated with that hedge in place (`--hedged`) only for prepaid
  annual terms; the calculator refuses `--hedged` for monthly billing, because a
  monthly price persists below the renewal trigger while the hedge covers one month.
- **Renewal repricing trigger (proposed).** If the SUI-cost basis at renewal is at or
  above **3×** the basis the quote used, the renewal is requoted with advance notice.
  The trigger and notice period are provisional terms requiring contract review
  before any customer is promised them.
- Payment fees of 3% plus $0.30 per invoice are provisional inputs, not a quote.

The 2026-10-23 [proposed price list](../BUSINESS_PLAN.md#proposed-price-list)
stands. Its $0.08 tier clears both floors at 1,000 and 10,000 devices; the volume
tiers below about $0.07 per device clear them only with the term hedge, which is
the main result below. Costs are Testnet-rescaled estimates until
[SF-135/#267](https://github.com/tyler-suifleet/suifleet/issues/267) measures a
Mainnet bill. Labor, support, taxes and customer acquisition are outside the
infrastructure margin used here.

## Where SUI enters the cost

| Cost line | Denominated in | Moves with SUI? | Notes |
|---|---|---|---|
| Transaction computation (health, receipts, archive anchors, enrollment, cleanup, Walrus register/certify gas) | SUI: gas units × reference gas price (RGP) | Yes, and with validator RGP votes | RGP is set each epoch by a stake-weighted validator survey, not pegged to USD |
| Storage deposits for on-chain objects | SUI: 76 MIST per unit | Working capital, 99.99% rebated on deletion | Only the 0.01% non-rebated part is recurring cost; the deposit is a SUI position |
| Walrus storage and writes | WAL, priced in USD terms by node operators | Weakly; adjusted periodically | Stress separately with the WAL multiple |
| RPC plans, SuiFleet full nodes, Seal providers | USD | No | The USD share grows with fleet size |
| Device gas float (0.06 SUI per device) | SUI | Working capital, not cost | Its USD value rises and falls with the token |

Verified reference values, read over gRPC with `LedgerService.GetEpoch`:

- Mainnet epoch 1291 (2026-10-25): reference gas price **100 MIST**; protocol version
  139 with `storage_gas_price` 76, `storage_rebate_rate` 9999 (99.99%) and
  `base_tx_cost_fixed` 1000.
- Reference gas price history: **1,000 MIST** at genesis (May 2023, per
  [sui.io/networkinfo](https://sui.io/networkinfo)); **549–557 MIST** from at least
  epoch 1050 (2026-02-25) through epoch 1105 (2026-04-21); **191 MIST** at epoch
  1110 (2026-04-26); **100 MIST** from epoch 1115 (2026-05-01) onward. Epochs
  between genesis and February 2026 were not sampled.
- SUI/USD (daily closes, Binance.US SUIUSDT from 2023-10-25; CoinGecko spot for
  2026-10-23 and 2026-10-25): $0.987 on 2026-10-23 (the quote basis), $0.92 on
  2026-10-25, low $0.43 (2023-10-31), high $5.29 (2025-01-04). CoinGecko's intraday
  extremes are $0.36 (2023-10-19) and $5.35 (2025-01-06).

Two consequences shape the model:

1. **A 10× move within one term has happened.** The largest rise within any
   365-day window of daily closes is **9.88×** (2024-08-05 $0.54 to 2025-01-04 $5.29);
   within 90 days, 5.0× (2024-09-06 to 2024-12-05); within 30 days, 2.4×
   (2024-09-01 to 2024-09-30). The largest falls are to 0.16× in 365 days
   (2025-07-27 $4.33 to 2026-07-27 $0.68), 0.35× in 90 days and 0.46× in 30 days.
   The 10× case is therefore a boundary that an annual term must survive, not a
   hypothetical tail.
2. **Validator gas-price votes move USD cost as much as the token does.** The
   April 2026 cut from about 550 to 100 MIST lowered the USD cost of a transaction
   5.5× in a week while SUI itself fell. A reversal would raise cost 5.5× with no
   token move. The model's SUI cost multiple is applied to the **USD cost** of
   SUI-denominated operations, so it covers token price, RGP and their product.
   The "implied SUI price" column assumes RGP stays at 100 MIST; read it as "or the
   equivalent RGP change".

## Model

All amounts are monthly USD for the whole fleet at its purchased workload.
`S` is SUI-sensitive recurring cost, `W` WAL expense, `O` other services, `p` the
payment fee fraction, `f` the fixed payment fee (divided by 12 on an annual
invoice), `m` the SUI cost multiple and `c` the fraction of the term's SUI spend
bought at invoice time.

```text
recurring cost(m)          = m × S + W + O
hedged recurring cost(m)   = (c + (1 − c) × m) × S + W + O
margin(m)                  = (price − recurring cost(m) − p × price − f) / price
price for margin g at m    = (recurring cost(m) + f) / (1 − g − p)
floor                      = max over policy floors of price for margin g_i at m_i
breakeven multiple         = (price × (1 − p) − f − W − O) / S
subscription               = round_up_to_cent(max(floor, monthly minimum, list price after any annual discount))
```

Unknown required costs refuse a quote; explicit zero costs need a stated reason.
Refundable deposits and gas float are reported as SUI-denominated working capital
and revalued in each scenario; they are never booked as recurring expense.
Prices round up to the cent so rounding never erodes a floor.

### Cost split

The typical profile from the business plan, 1,000 devices, costs about $9 a month
at the 2026-10-23 basis (SUI $0.987, RGP 100 MIST). Its split by exposure:

| Component | Monthly, 1,000 devices | Exposure |
|---|---:|---|
| Daily on-chain health, $0.003 per device | $3.00 | SUI |
| Four deployment receipts, $0.0014 per device | $1.40 | SUI |
| Weekly log archives kept 6 weeks, $0.004 per device: anchors and non-rebated storage | ~$1.10 | SUI |
| Retry and failed-write allowance | ~$0.50 | SUI |
| **SUI-sensitive total `S`** | **$6.00** | |
| Weekly log archives: Walrus storage and writes | ~$2.90 | WAL |
| Four 50 MB release uploads per fleet | ~$0.12 | WAL |
| **Walrus total `W`** | **$3.00** | |
| RPC on the public endpoint or Chainstack free tier | $0 | USD |

At 10,000 devices the network lines scale linearly and a paid RPC plan adds $49
(`O`); at 100,000 devices one SuiFleet-run full node adds about $570. The SUI
share of recurring cost is therefore 67% at 1,000 devices, 43% at 10,000 and 41%
at 100,000. These splits are estimates derived from Testnet measurements rescaled
to Mainnet gas prices; SF-135 replaces them.

## Results at the proposed prices

### 1,000 devices at $80 a month ($0.08 per device)

Floors: $45.32 (50% at 3×) and $65.26 (breakeven at 10×). The list price prevails
at **$80.00**. Breakeven is **12.4× SUI cost** (SUI ≈ $12.22 at today's RGP);
50% margin holds up to **5.7×** (≈ $5.64, roughly the January 2025 high). The
hedge costs $6 of SUI a month, or $72 bought up front on a $864 annual invoice.

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $4.50 | $72.80 | 91.0% | 85.4% | $22.30 |
| 0.5× | $0.49 | $6.00 | $71.30 | 89.1% | 85.4% | $44.61 |
| 1× | $0.99 | $9.00 | $68.30 | 85.4% | 85.4% | $89.22 |
| 2× | $1.97 | $15.00 | $62.30 | 77.9% | 85.4% | $178.44 |
| 3× | $2.96 | $21.00 | $56.30 | 70.4% | 85.4% | $267.66 |
| 5× | $4.94 | $33.00 | $44.30 | 55.4% | 85.4% | $446.10 |
| 10× | $9.87 | $63.00 | $14.30 | 17.9% | 85.4% | $892.20 |
| 20× | $19.74 | $123.00 | -$45.70 | -57.1% | 85.4% | $1,784.40 |
| 1× SUI, 2× WAL | $0.99 | $12.00 | $65.30 | 81.6% | 81.6% | $89.22 |

"Margin" is the unhedged outcome if SUI cost moves during the term. "Hedged
margin" is the outcome when the term's SUI was bought at invoice; it stays at the
1× value because the paid-for operations no longer depend on the token. "SUI-
denominated capital" is the USD value of the fleet's storage deposits and gas
float (90.4 SUI); it moves with the token in the opposite direction to margin.

### 10,000 devices at $800 a month ($0.08 per device)

Floors: $551.71 and $700.31; the list price prevails at **$800.00**. Breakeven is
**11.6×** (≈ $11.46); 50% margin holds to **4.9×** (≈ $4.88). The paid RPC plan
is USD-denominated, which lowers the SUI share to 43% but also lowers the margin
at every multiple. The hedge is $60 a month or $720 a year.

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $94.00 | $681.70 | 85.2% | 79.6% | $223.05 |
| 0.5× | $0.49 | $109.00 | $666.70 | 83.3% | 79.6% | $446.10 |
| 1× | $0.99 | $139.00 | $636.70 | 79.6% | 79.6% | $892.20 |
| 2× | $1.97 | $199.00 | $576.70 | 72.1% | 79.6% | $1,784.40 |
| 3× | $2.96 | $259.00 | $516.70 | 64.6% | 79.6% | $2,676.60 |
| 5× | $4.94 | $379.00 | $396.70 | 49.6% | 79.6% | $4,461.00 |
| 10× | $9.87 | $679.00 | $96.70 | 12.1% | 79.6% | $8,922.00 |
| 20× | $19.74 | $1,279.00 | -$503.30 | -62.9% | 79.6% | $17,844.00 |
| 1× SUI, 2× WAL | $0.99 | $169.00 | $606.70 | 75.8% | 75.8% | $892.20 |

### 100,000 devices: the volume tier is where SUI risk bites

The proposed volume price averages $5,300 a month ($0.053 per device) against a
$1,470 cost. Without a hedge, the 10× breakeven floor is **$7,082.79** ($0.0708
per device) and binds: at $5,300 the fleet would lose money from **7.1× SUI cost**
(≈ $7.03) and drop below 50% margin from **2.7×** (≈ $2.67), both inside SUI's
observed 12-month range. At the floor price:

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $1,020.00 | $5,850.01 | 82.6% | 76.2% | $2,230.50 |
| 0.5× | $0.49 | $1,170.00 | $5,700.01 | 80.5% | 76.2% | $4,461.00 |
| 1× | $0.99 | $1,470.00 | $5,400.01 | 76.2% | 76.2% | $8,922.00 |
| 2× | $1.97 | $2,070.00 | $4,800.01 | 67.8% | 76.2% | $17,844.00 |
| 3× | $2.96 | $2,670.00 | $4,200.01 | 59.3% | 76.2% | $26,766.00 |
| 5× | $4.94 | $3,870.00 | $3,000.01 | 42.4% | 76.2% | $44,610.00 |
| 10× | $9.87 | $6,870.00 | $0.01 | 0.0% | 76.2% | $89,220.00 |
| 20× | $19.74 | $12,870.00 | -$5,999.99 | -84.7% | 76.2% | $178,440.00 |
| 1× SUI, 2× WAL | $0.99 | $1,770.00 | $5,100.01 | 72.0% | 72.0% | $8,922.00 |

With an annual prepaid term and the hedge (`--annual --hedged`), the floors fall to
$3,127.72 and $1,515.49, so the discounted list price of **$4,770.00** a month
($57,240 a year, $0.0477 per device) prevails. SuiFleet buys $7,200 of SUI when
the invoice is paid (12.6% of the invoice) and the margin is 66.2% whatever SUI
does during the year. The "Margin" column shows what the same price would earn
without the hedge:

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $1,020.00 | $3,606.88 | 75.6% | 66.2% | $2,230.50 |
| 0.5× | $0.49 | $1,170.00 | $3,456.88 | 72.5% | 66.2% | $4,461.00 |
| 1× | $0.99 | $1,470.00 | $3,156.88 | 66.2% | 66.2% | $8,922.00 |
| 2× | $1.97 | $2,070.00 | $2,556.88 | 53.6% | 66.2% | $17,844.00 |
| 3× | $2.96 | $2,670.00 | $1,956.88 | 41.0% | 66.2% | $26,766.00 |
| 5× | $4.94 | $3,870.00 | $756.88 | 15.9% | 66.2% | $44,610.00 |
| 10× | $9.87 | $6,870.00 | -$2,243.12 | -47.0% | 66.2% | $89,220.00 |
| 20× | $19.74 | $12,870.00 | -$8,243.12 | -172.8% | 66.2% | $178,440.00 |
| 1× SUI, 2× WAL | $0.99 | $1,770.00 | $2,856.88 | 59.9% | 59.9% | $8,922.00 |

**Policy consequence.** Volume prices below about $0.07 per device are offered
only on prepaid annual terms with the hedge, or to customers who fund their own
network costs in SUI (below). A month-to-month volume fleet pays the floor.

## What it means for customers

- **The price is fixed for the term.** A SUI move during a paid month or year
  changes SuiFleet's margin, never the customer's invoice. There is no usage
  overage and no mid-term adjustment.
- **Renewal repricing is bounded and disclosed.** If the SUI-cost basis at renewal
  is at or above 3× the quote basis (about $2.96 at today's RGP, or an equivalent
  RGP change), the renewal is requoted with at least 30 days' notice. At 3× the
  $0.08 tier still earns 70% at 1,000 devices and 65% at 10,000, so the trigger
  rarely forces a price change at that tier; it exists for the volume tiers and
  for larger moves. Below the trigger, renewals keep their price.
- **Even a repriced subscription stays well under the AWS comparison.** The price
  that holds 50% margin at 10× SUI cost is $134.69 for 1,000 devices ($0.135 per
  device), 48% below Greengrass's $259 for the same workload. A 50%-margin price
  would only reach Greengrass parity at about 20× SUI cost (1,000 devices) or 18×
  (100,000 devices).
- **Customers who want to carry SUI risk themselves can.** The fleet budget
  ([SF-083](../FLEET_BUDGET.md)) already lets a fleet owner prepay network costs in
  SUI. Under that arrangement SuiFleet charges a USD platform fee for the software
  and the owner's own SUI pays the network; the owner's USD cost of the network
  share then moves with the token (about $0.009 per device a month at 1×, $0.09
  at 10×) and SuiFleet's margin does not. This suits large fleets that hold SUI;
  it is not the default offer.

## What it means for margins and revenue

- **Revenue is fixed in USD.** Only cost moves. Gross profit per 1,000 devices at
  the $0.08 tier is $68 a month at 1×, $56 at 3×, $44 at 5× and $14 at 10×. Churn
  risk appears only if a renewal repricing is triggered.
- **Headroom shrinks with volume discounts.** Breakeven is 12.4× at $0.08 for
  1,000 devices, 11.6× at 10,000 (USD-denominated RPC lowers the base margin) and
  7.1× at the $0.053 volume average. The hedge restores the volume tier.
- **The hedge is cheap relative to pricing for the shock.** Buying the term's SUI
  costs 7.5% of the invoice at the $0.08 tier and 12.6% at the volume tier, paid
  from the invoice itself. Pricing instead for a guaranteed 80% margin at 10× would
  mean $372.35 for 1,000 devices, $0.37 per device, above Greengrass's $0.26.
- **The hedge's downside is bounded.** If SUI halves after purchase, the pre-bought
  SUI still pays the same operations; SuiFleet simply paid $6 for what would have
  cost $3 per 1,000 devices that month, 3.75% of the invoice. If SUI rises, the
  hedge is exactly what prevents the loss.
- **Working capital moves the other way.** The fleet's deposits and gas float
  (about 0.09 SUI per device, $89 per 1,000 devices today) are worth $892 per
  1,000 devices at 10× and $45 at 0.5×. That is a natural partial hedge on the
  way up and a small capital loss on the way down; it is not revenue either way.
- **A fall in SUI widens margins and should not trigger a price cut.** Validators
  may vote RGP up again, and the working capital and any pre-bought SUI are worth
  less in USD. Keep list prices; use the room for the entry offers and design
  partners already in the plan.
- **Walrus is a smaller, separate exposure.** Doubling WAL cost takes about 4
  points of margin at 1,000 devices and 4 points at 100,000.

## Operating the policy

1. **Quote** with a copy of an example file: the customer's device count and
   settings, the current SUI price and RGP in `basis`, dated sources, the list
   price, minimum and any discount, and the Greengrass comparison for the same
   workload. List missing costs in `unquoted_costs`; the calculator refuses a
   price until they are supplied. Set `basis.kind` honestly.
2. **Check the floors** without the hedge for monthly billing, and with
   `--annual --hedged` for prepaid annual terms. The binding constraint is
   reported; a volume price that only clears the floors hedged is sold only as
   prepaid annual.
3. **Hedge at invoice.** Buy the term's SUI-sensitive spend (`hedge_purchase_usd`)
   when the payment settles and hold it for that fleet's operations. Treasury
   custody and accounting for that SUI are a separate operational decision; the
   calculator only sizes the purchase.
4. **Recalculate** before each renewal and whenever SUI or RGP has moved 2× from
   the last quote basis, so the renewal trigger is evaluated on current numbers.
5. **Reprice only at renewal**, with notice, when the trigger is met, and show the
   customer the price that restores the target margin (`price_for_margin`).
6. **Never** change a customer's live price, the dApp price table, on-chain
   spending caps or fleet-budget deposits from this planner. It sizes a reviewed
   tariff; subscription collection and production-price acceptance remain separate.

## Limits of this analysis

- The SUI/WAL/USD split is an estimate from Testnet gas rescaled to Mainnet's
  100 MIST reference price; the SUI share (67% at 1,000 devices) is the largest
  uncertainty and directly scales every multiple. SF-135 measures it.
- "Implied SUI price" assumes the reference gas price stays at 100 MIST. The
  multiple is on USD cost, so the same row also describes an RGP vote.
- Monthly billing is priced unhedged. A monthly hedge would cover only the paid
  month while the price carries over below the trigger, so the unhedged floors are
  the monthly protection and exposure is bounded by the repricing notice period.
- The hedge is bought at the price on the day the invoice settles, not the quote
  basis. A move between quote and payment is not hedged, and the purchased SUI
  carries custody, accounting and opportunity-cost risk that this planner does not
  model. No hedge purchase is approved by this document.
- Payment fees, the 10% annual discount and the $49 minimum are the plan's
  provisional terms, not processor or legal quotes.
- Labor, support, taxes and customer acquisition are excluded; the margins are
  infrastructure margins.
- No live tariff, dApp configuration, wallet, spending allowance or customer
  subscription changes with this document.

## Reproduce

```bash
python3 scripts/subscription_pricing.py docs/business/subscription-example.json
python3 scripts/subscription_pricing.py docs/business/subscription-example.json --table
python3 scripts/subscription_pricing.py docs/business/subscription-example-10k.json --table
python3 scripts/subscription_pricing.py docs/business/subscription-example-100k.json --table
python3 scripts/subscription_pricing.py docs/business/subscription-example-100k.json --annual --hedged --table
python3 scripts/subscription_pricing.py docs/business/subscription-example.json --policy my-policy.json
python3 -m unittest discover -s scripts/tests -p test_subscription_pricing.py -v
```

The test suite checks the arithmetic, the fail-closed input handling, a randomized
property that floors hold their margin whenever they bind, and that the four
tables in this document match the calculator's output.

Walrus documents [USD-targeted storage pricing](https://docs.wal.app/docs/system-overview/storage-costs)
and [periodic operator price adjustments](https://docs.wal.app/docs/operator-guide/storage-nodes/storage-node-setup#pricing-currency-frost-and-nanousd),
which supports the independent WAL multiple. Sui's [gas model](https://docs.sui.io/concepts/tokenomics/gas-in-sui)
sets fees in MIST through the validator gas-price survey with no USD peg.
