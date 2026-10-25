# Subscription pricing under SUI cost stress

Decision recorded 2026-10-25: derive the fixed subscription floor from a multiple
of SUI-sensitive recurring cost. Customers buy device capacity and bounded
reporting, data and retention settings, with no automatic usage-overage invoices.

The [planning policy](subscription-policy.json) currently uses a **10× SUI-cost
multiple** and an **80% infrastructure margin target at that stressed cost**.
The user requested the 10× exposure case. The 80% margin target remains a
configurable planning assumption; final tariffs are provisional and margins are
not guaranteed. Final prices still require the measured production bill in
SF-135/#267. Labor, support, taxes
and customer acquisition are outside this infrastructure margin.

## Formula and cost classification

All amounts below are monthly USD for the whole fleet at its purchased workload:

- `S`: SUI-sensitive recurring consumption, including health, deployment receipts,
  enrollment/rotation attributable to that period, archive anchors, cleanup,
  retries, Walrus registration/certification/renewal gas and SUI-denominated
  relay/provider charges. Classify by fee exposure, not product name. Convert
  net effects to USD at a recorded SUI reference price; include computation and
  nonrefundable storage. Do not assume failed writes are free.
- `W`: WAL-funded storage/write expense in USD, with encoded sizes and purchased
  retention included. Separate the SUI transactions for those operations into `S`.
- `O`: other required recurring services, including paid RPC, delivery, hosting
  or Seal providers where applicable. An unknown price is unquoted, not zero.
- `mS`, `mW`: independent cost-stress multipliers (defaults **10** and **1**).
  `mS` stresses total SUI-denominated cost, covering token appreciation and/or
  higher fees per operation; it does not separately multiply both by 10 and accidentally
  apply a 100× shock. `mW = 1` assumes Walrus's USD-targeted pricing holds; use a
  higher value to examine repricing delays or other WAL-cost exposure.
- `g`: stressed infrastructure margin target (**0.80**); `p`: payment fee fraction
  (**0.03**); `f`: fixed payment fee per invoice (**$0.30**, divided by 12 for an
  annual invoice). Payment fees are provisional inputs, not a processor quote.

```text
stressed recurring cost = mS × S + mW × W + O
risk floor = (stressed recurring cost + f) / (1 − g − p)
effective monthly subscription = round_up_to_cent(max(
    risk floor,
    monthly minimum,
    commercial tier quote after any annual discount
))
```

The denominator must be positive. Discounts, volume tiers and partner prices
cannot reduce the effective paid amount below the risk floor or monthly minimum.
The annual invoice is 12 times that effective monthly amount. Quote this final
amount before purchase; do not advertise an unconditional discount that the floor
would prevent. A lower cost of service at scale can justify a lower per-device
price, but the **whole-fleet floor still applies**.

Refundable storage deposits and device gas float are working capital, reported
separately. Count the unrecoverable part and actual funding/financing costs where
applicable, without booking the whole deposit or float as monthly consumption.
One-time activation pricing must separately cover setup expense and capital needs;
it is not calculated by this recurring-subscription tool. Only rely on reclaimed
deposits when the corresponding cleanup path and retention assumptions are proven.

## Reproducible example and sensitivity

The example deliberately uses **synthetic**, fully specified amounts: 1,000
devices, `S = $8`, `W = $0.50`, `O = $0.50`, an $80 commercial candidate and $49
minimum. It does **not** claim that the earlier $9 production-cost estimate has
this split. Actual inputs need dated evidence for the chosen workload.

```bash
python3 scripts/subscription_pricing.py docs/business/subscription-example.json
python3 scripts/subscription_pricing.py docs/business/subscription-example.json --annual
python3 -m unittest discover -s scripts/tests -p test_subscription_pricing.py -v
```

The monthly subscription floor is **$478.24**. It includes card-fee allowance and
rounding. Its sensitivity at that same fixed customer price is:

| SUI cost multiple | WAL cost multiple | Recurring cost | Infrastructure margin after payment fees |
|---:|---:|---:|---:|
| 1× | 1× | $9.00 | 95.06% |
| 2× | 1× | $17.00 | 93.38% |
| 3× | 1× | $25.00 | 91.71% |
| 5× | 1× | $41.00 | 88.36% |
| 10× | 1× | $81.00 | 80.00% |
| 20× | 1× | $161.00 | 63.27% |
| 1× | 2× | $9.50 | 94.95% |

With the example's requested 10% annual discount, the risk floor overrides the
$72 commercial candidate. Amortizing the single fixed card fee produces **$476.62
per month / $5,719.44 annually**, rather than applying 10% below a safe floor.
These amounts demonstrate the policy; they are not a new customer price list.
The 10× row assumes SUI-denominated fees per operation stay unchanged while SUI
appreciates tenfold. The multiplier applies to total SUI-sensitive cost, so a
simultaneous 2× fee increase would produce 20× exposure; that row shows the
remaining margin rather than promising the 80% target beyond the selected buffer.

## Preparing a customer plan

Use a copy of the example with the customer's device count, deployment frequency,
health cadence, bounded log/data volume, batching, retention, retry allowance and
production service choices. Attach the workload calculation, reference exchange
prices, fee observations and source dates in `basis.sources`. Set `basis.kind`
honestly to `illustrative`, `estimated` or `measured`. List missing required costs
in `unquoted_costs`; the calculator refuses a price until those are supplied.
Explicit zero costs need evidence, such as a deliberately excluded service.

Use `--policy PATH` to examine other multiples, margin targets or payment fees.
Recalculate when the workload, token reference price, network fees or provider
terms change, and before issuing/renewing a subscription. A shock beyond the
assumptions can reduce margins; the multiplier is a buffer, not a dollar peg.
Honor purchased terms and existing data/expiry protections; any proposed price
change needs advance customer disclosure, not a surprise usage invoice.

The Settings price table remains the historical commercial candidate. This offline
planner produces the minimum for a reviewed tariff; it does not install a new
`NEXT_PUBLIC_FLEET_PRICE_TABLE`, change a customer's subscription, collect payment,
fund wallets or increase on-chain spending caps. Existing subscription collection
and production-price acceptance requirements remain separate.

Walrus documents [USD-targeted storage pricing](https://docs.wal.app/docs/system-overview/storage-costs)
and [periodic operator price adjustments](https://docs.wal.app/docs/operator-guide/storage-nodes/storage-node-setup#pricing-currency-frost-and-nanousd).
That supports the independent WAL assumption, but does not guarantee immediate
repricing or include Sui gas. Sui's [fee mechanism](https://www.docs.sui.io/develop/transaction-payment/gas-in-sui)
does not provide an immediate USD peg. Recheck actual network quotes before use.
