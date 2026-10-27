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
  term's SUI-sensitive operations will consume at the basis gas price
  (`term_sui_purchased_fraction` = 1). That fixes the token price for that quantity
  of SUI only: if validators raise the reference gas price, or storage price or gas
  per operation rises, the extra SUI is bought at the then-current price. Floors may
  be evaluated with the hedge (`--hedged`) only for prepaid annual terms; the
  calculator refuses `--hedged` for monthly billing, because a monthly price
  persists below the renewal trigger while the hedge covers one month. A hedged
  floor assumes the whole multiple may be a quantity rise unless the floor sets
  `max_token_quantity_multiple`; the policy sets no such bound, so hedged and
  unhedged floors are equal today.
- **Renewal repricing trigger (proposed).** If the SUI-cost basis at renewal is at or
  above **3×** the basis the quote used, the renewal is requoted with advance notice.
  The trigger and notice period are provisional terms requiring contract review
  before any customer is promised them.
- Payment fees of 3% plus $0.30 per invoice are provisional inputs, not a quote.

The 2026-10-23 [proposed price list](../BUSINESS_PLAN.md#proposed-price-list)
stands. Its $0.08 tier clears both floors at 1,000 and 10,000 devices. The volume
tiers below about $0.07 per device do not clear them, with or without the term
hedge, because the hedge cannot cover a gas-price rise; this is the main result
below. Costs are Testnet-rescaled estimates until
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
   token move, and a return to the 1,000 MIST genesis price would raise it 10×.
   The model's SUI cost multiple is applied to the **USD cost** of SUI-denominated
   operations: it is the token price multiple times the token quantity multiple
   (RGP, storage price and gas per operation). Unhedged, only the product matters.
   A hedge holds a fixed quantity of SUI, so it neutralises the token price part
   only; the model therefore reports hedged outcomes for token and quantity moves
   separately. The "implied SUI price" and capital columns read the multiple as a
   token move.

## Model

All amounts are monthly USD for the whole fleet at its purchased workload.
`S` is SUI-sensitive recurring cost, `W` WAL expense, `O` other services, `p` the
payment fee fraction, `f` the fixed payment fee (divided by 12 on an annual
invoice), `t` the token price multiple, `q` the token quantity multiple, `m = t × q`
the SUI cost multiple and `c` the fraction of the term's basis SUI quantity bought
at invoice time.

```text
recurring cost(m)          = m × S + W + O
hedged recurring cost(t,q) = (c + max(q − c, 0) × t) × S + W + O
hedged floor at m          = hedged cost at q = min(max_token_quantity_multiple, m), t = m / q
margin(m)                  = (price − recurring cost(m) − p × price − f) / price
price for margin g at m    = (recurring cost(m) + f) / (1 − g − p)
floor                      = max over policy floors of price for margin g_i at m_i
breakeven multiple         = (price × (1 − p) − f − W − O) / S
subscription               = round_up_to_cent(max(floor, monthly minimum, list price after any annual discount))
```

The hedged cost charges the full purchase even if the workload needs less SUI, and
it rises with the quantity share of a given multiple, so the hedged floor uses the
largest quantity the floor allows (by default all of `m`). A token-only move
(`q = 1`, `c = 1`) leaves hedged cost at 1×; a quantity-only move (`t = 1`) costs
exactly what it would unhedged once `q` exceeds `c`. The policy's
`combined_shocks` list reports explicit token × quantity cases.

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
hedge costs $6 of SUI a month, or $72 bought up front on the $864 annual invoice
(8.3% of it).

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin, token price move | Hedged margin, gas/quantity move | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $4.50 | $72.80 | 91.0% | 85.4% | 85.4% | $22.30 |
| 0.5× | $0.49 | $6.00 | $71.30 | 89.1% | 85.4% | 85.4% | $44.61 |
| 1× | $0.99 | $9.00 | $68.30 | 85.4% | 85.4% | 85.4% | $89.22 |
| 2× | $1.97 | $15.00 | $62.30 | 77.9% | 85.4% | 77.9% | $178.44 |
| 3× | $2.96 | $21.00 | $56.30 | 70.4% | 85.4% | 70.4% | $267.66 |
| 5× | $4.94 | $33.00 | $44.30 | 55.4% | 85.4% | 55.4% | $446.10 |
| 10× | $9.87 | $63.00 | $14.30 | 17.9% | 85.4% | 17.9% | $892.20 |
| 20× | $19.74 | $123.00 | -$45.70 | -57.1% | 85.4% | -57.1% | $1,784.40 |
| 1× SUI, 2× WAL | $0.99 | $12.00 | $65.30 | 81.6% | 81.6% | 81.6% | $89.22 |

| Token price × quantity | SUI cost multiple | Recurring cost | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|
| 10× × 1× | 10× | $63.00 | 17.9% | 85.4% | $892.20 |
| 1× × 5.5× | 5.5× | $36.00 | 51.6% | 51.6% | $89.22 |
| 1× × 10× | 10× | $63.00 | 17.9% | 17.9% | $89.22 |
| 2× × 5× | 10× | $63.00 | 17.9% | 25.4% | $178.44 |

"Margin" is the unhedged outcome if SUI cost moves during the term. The hedged
columns assume the term's SUI was bought at invoice (shown at the same price for
comparison; the hedge applies only to annual terms). If the move is in the token
price, hedged margin stays at its 1× value. If it is in the quantity of SUI the
workload needs (a gas-price vote), the extra SUI is bought at market and hedged
margin falls exactly as unhedged margin does. The second table combines both;
`2× × 5×` means the token doubles and RGP rises 5×. "SUI-denominated capital" is
the USD value of the fleet's storage deposits and gas float (90.4 SUI) if the
token moves; a gas-price change leaves it unchanged.

### 10,000 devices at $800 a month ($0.08 per device)

Floors: $551.71 and $700.31; the list price prevails at **$800.00**. Breakeven is
**11.6×** (≈ $11.46); 50% margin holds to **4.9×** (≈ $4.88). The paid RPC plan
is USD-denominated, which lowers the SUI share to 43% but also lowers the margin
at every multiple. The hedge is $60 a month, or $720 of the $8,640 annual invoice
(8.3%).

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin, token price move | Hedged margin, gas/quantity move | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $94.00 | $681.70 | 85.2% | 79.6% | 79.6% | $223.05 |
| 0.5× | $0.49 | $109.00 | $666.70 | 83.3% | 79.6% | 79.6% | $446.10 |
| 1× | $0.99 | $139.00 | $636.70 | 79.6% | 79.6% | 79.6% | $892.20 |
| 2× | $1.97 | $199.00 | $576.70 | 72.1% | 79.6% | 72.1% | $1,784.40 |
| 3× | $2.96 | $259.00 | $516.70 | 64.6% | 79.6% | 64.6% | $2,676.60 |
| 5× | $4.94 | $379.00 | $396.70 | 49.6% | 79.6% | 49.6% | $4,461.00 |
| 10× | $9.87 | $679.00 | $96.70 | 12.1% | 79.6% | 12.1% | $8,922.00 |
| 20× | $19.74 | $1,279.00 | -$503.30 | -62.9% | 79.6% | -62.9% | $17,844.00 |
| 1× SUI, 2× WAL | $0.99 | $169.00 | $606.70 | 75.8% | 75.8% | 75.8% | $892.20 |

| Token price × quantity | SUI cost multiple | Recurring cost | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|
| 10× × 1× | 10× | $679.00 | 12.1% | 79.6% | $8,922.00 |
| 1× × 5.5× | 5.5× | $409.00 | 45.8% | 45.8% | $892.20 |
| 1× × 10× | 10× | $679.00 | 12.1% | 12.1% | $892.20 |
| 2× × 5× | 10× | $679.00 | 12.1% | 19.6% | $1,784.40 |

### 100,000 devices: the volume tier is where SUI risk bites

The proposed volume price averages $5,300 a month ($0.053 per device) against a
$1,470 cost. Without a hedge, the 10× breakeven floor is **$7,082.79** ($0.0708
per device) and binds: at $5,300 the fleet would lose money from **7.1× SUI cost**
(≈ $7.03) and drop below 50% margin from **2.7×** (≈ $2.67), both inside SUI's
observed 12-month range. At the floor price:

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin, token price move | Hedged margin, gas/quantity move | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $1,020.00 | $5,850.01 | 82.6% | 76.2% | 76.2% | $2,230.50 |
| 0.5× | $0.49 | $1,170.00 | $5,700.01 | 80.5% | 76.2% | 76.2% | $4,461.00 |
| 1× | $0.99 | $1,470.00 | $5,400.01 | 76.2% | 76.2% | 76.2% | $8,922.00 |
| 2× | $1.97 | $2,070.00 | $4,800.01 | 67.8% | 76.2% | 67.8% | $17,844.00 |
| 3× | $2.96 | $2,670.00 | $4,200.01 | 59.3% | 76.2% | 59.3% | $26,766.00 |
| 5× | $4.94 | $3,870.00 | $3,000.01 | 42.4% | 76.2% | 42.4% | $44,610.00 |
| 10× | $9.87 | $6,870.00 | $0.01 | 0.0% | 76.2% | 0.0% | $89,220.00 |
| 20× | $19.74 | $12,870.00 | -$5,999.99 | -84.7% | 76.2% | -84.7% | $178,440.00 |
| 1× SUI, 2× WAL | $0.99 | $1,770.00 | $5,100.01 | 72.0% | 72.0% | 72.0% | $8,922.00 |

| Token price × quantity | SUI cost multiple | Recurring cost | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|
| 10× × 1× | 10× | $6,870.00 | 0.0% | 76.2% | $89,220.00 |
| 1× × 5.5× | 5.5× | $4,170.00 | 38.1% | 38.1% | $8,922.00 |
| 1× × 10× | 10× | $6,870.00 | 0.0% | 0.0% | $8,922.00 |
| 2× × 5× | 10× | $6,870.00 | 0.0% | 8.5% | $17,844.00 |

The hedge does not lower these floors. Buying the term's SUI at invoice fixes the
token price, but an RGP rise multiplies the SUI the workload needs and the extra is
bought at market, so the 10× floor still has to survive a 10× quantity rise (RGP
back at the 1,000 MIST genesis value). With the policy as written, the prepaid
annual term (`--annual --hedged`) is floored at **$7,082.50** a month, the same as
unhedged; its hedge would buy $7,200 of SUI, 8.5% of the $84,990 invoice.

The $4,770.00 discounted annual list price ($57,240 a year, $0.0477 per device)
clears the floors only if the token quantity is held at the basis, which is what
`max_token_quantity_multiple` = 1 asserts (floors $3,127.72 and $1,515.49). The
table shows what that price earns: 66.2% through any token move, but break-even at
a **6.3× gas-price rise** and **9.6% margin if RGP returned to April 2026's
~550 MIST** (5.5×). SuiFleet would buy $7,200 of SUI, 12.6% of the invoice:

| SUI cost multiple | Implied SUI price | Recurring cost | Gross profit | Margin | Hedged margin, token price move | Hedged margin, gas/quantity move | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.25× | $0.25 | $1,020.00 | $3,606.88 | 75.6% | 66.2% | 66.2% | $2,230.50 |
| 0.5× | $0.49 | $1,170.00 | $3,456.88 | 72.5% | 66.2% | 66.2% | $4,461.00 |
| 1× | $0.99 | $1,470.00 | $3,156.88 | 66.2% | 66.2% | 66.2% | $8,922.00 |
| 2× | $1.97 | $2,070.00 | $2,556.88 | 53.6% | 66.2% | 53.6% | $17,844.00 |
| 3× | $2.96 | $2,670.00 | $1,956.88 | 41.0% | 66.2% | 41.0% | $26,766.00 |
| 5× | $4.94 | $3,870.00 | $756.88 | 15.9% | 66.2% | 15.9% | $44,610.00 |
| 10× | $9.87 | $6,870.00 | -$2,243.12 | -47.0% | 66.2% | -47.0% | $89,220.00 |
| 20× | $19.74 | $12,870.00 | -$8,243.12 | -172.8% | 66.2% | -172.8% | $178,440.00 |
| 1× SUI, 2× WAL | $0.99 | $1,770.00 | $2,856.88 | 59.9% | 59.9% | 59.9% | $8,922.00 |

| Token price × quantity | SUI cost multiple | Recurring cost | Margin | Hedged margin | SUI-denominated capital |
|---:|---:|---:|---:|---:|---:|
| 10× × 1× | 10× | $6,870.00 | -47.0% | 66.2% | $89,220.00 |
| 1× × 5.5× | 5.5× | $4,170.00 | 9.6% | 9.6% | $8,922.00 |
| 1× × 10× | 10× | $6,870.00 | -47.0% | -47.0% | $8,922.00 |
| 2× × 5× | 10× | $6,870.00 | -47.0% | -34.4% | $17,844.00 |

**Policy consequence.** Volume prices below about $0.07 per device are offered only
to customers who fund their own network costs in SUI (below), whichever term they
choose; other volume fleets, monthly or prepaid annual, pay the floor. A
contractual network-fee adjustment would bound the quantity exposure and let the
hedged floors use a quantity bound, but it would change the price mid-term, which
this policy rules out; it is not proposed here and would need contract review.

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
  7.1× at the $0.053 volume average. The hedge does not restore the volume tier,
  because it leaves gas-price exposure in place.
- **The hedge protects against the token, not the gas price.** Buying the term's
  SUI costs 8.3% of the annual invoice at the $0.08 tier and 12.6% of a $4,770
  volume invoice, paid from the invoice itself. It keeps margin at its 1× value
  through any token move, but a validator RGP vote raises the SUI needed and the
  extra is bought at market. The unhedged floors are therefore the protection
  against gas-price moves on every term. Pricing instead for a guaranteed 80%
  margin at 10× would mean $372.35 for 1,000 devices, $0.37 per device, above
  Greengrass's $0.26.
- **The hedge's downside is bounded.** If SUI halves after purchase, or RGP falls,
  the pre-bought SUI still pays the same operations; SuiFleet simply paid $6 for
  what would have cost $3 per 1,000 devices that month, 4.2% of the $72 monthly
  share of the annual invoice. If SUI rises, the hedge is exactly what prevents
  the loss.
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
   reported. Do not set `max_token_quantity_multiple` below the floor's multiple
   unless a reviewed contract term actually bounds gas-price exposure.
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
  multiple is on USD cost, so the unhedged columns of the same row also describe an
  RGP vote; the hedged columns and capital value differ between the two.
- Unused pre-bought SUI gets no credit when the workload needs less than was
  bought, which is conservative when SUI rises and RGP falls together.
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
# With a my-policy.json whose floors set "max_token_quantity_multiple": "1", the last
# command shows the $4,770 table above.
python3 scripts/subscription_pricing.py docs/business/subscription-example.json --policy my-policy.json
python3 -m unittest discover -s scripts/tests -p test_subscription_pricing.py -v
```

The test suite checks the arithmetic, token-only, gas-price-only and combined
shocks against the hedge, the fail-closed input handling, a randomized property
that floors hold their margin under every token × quantity split they allow, and
that the four tables in this document match the calculator's output. The last
table uses the policy with `max_token_quantity_multiple` = 1 on both floors.

Walrus documents [USD-targeted storage pricing](https://docs.wal.app/docs/system-overview/storage-costs)
and [periodic operator price adjustments](https://docs.wal.app/docs/operator-guide/storage-nodes/storage-node-setup#pricing-currency-frost-and-nanousd),
which supports the independent WAL multiple. Sui's [gas model](https://docs.sui.io/concepts/tokenomics/gas-in-sui)
sets fees in MIST through the validator gas-price survey with no USD peg.

Public pricing is generated from the console price table and this policy. See
[public pricing regeneration](PUBLIC_PRICING.md) for the source files, regeneration
commands and required freshness/build checks.
