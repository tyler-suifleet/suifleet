# Public pricing generation

The landing page is a generated proposal, not a live tariff or a deployment.
`dapp/src/lib/fleet-price-table.json` is the canonical list-price table consumed
by the console (`fleet-pricing.ts`, including validation and optional deployment
overrides) and the offline landing-page generator. It includes graduated tiers,
minimum, activation, annual discount, data allowance and settings/add-on prices.
`subscription-policy.json` schema 2 supplies the term hedge, renewal trigger and
notice period. The console's optional environment override does not change the
public proposal; quotes must identify their configured prices.

When changing the proposal in `docs/BUSINESS_PLAN.md`:

1. Update the shared price table and/or subscription policy to match the approved
   proposal. Do not adjust prices merely to make a test pass.
2. Run `python3 scripts/subscription_pricing.py --generate-site` and review
   `site/src/pages/pricing.html`. The embedded business-plan digest makes any plan
   edit require regeneration, including copy-only edits.
3. Run `python3 scripts/subscription_pricing.py --check-site` and
   `python3 -m unittest discover -s scripts/tests -p test_subscription_pricing.py -v`.
   Normal pricing CI runs this suite; stale output fails. The existing planning
   CLI and schema-2 examples remain unchanged.
4. Run `python3 site/build.py --out /tmp/suifleet-pricing-site` followed by
   `python3 site/check.py /tmp/suifleet-pricing-site`. Review the rendered copy
   against the business plan. Run frontend typecheck/build and estimator tests
   for table changes. Commit source and regenerated page together.

SF-178 uses the SF-172 committed site interface at
`d5306c76179be15a09d5ae25740019f383a46a24`: ordinary page HTML with `app` and `repo`
tokens. It changes no build/layout API. SF-172 must merge before SF-178 integrates.
The page deliberately describes default platform/data/settings pricing rather
than duplicating additional hand-maintained managed-plan or entry-offer numbers.
SUI and WAL network estimates remain separate from subscription prices; the
all-in proposal and customer-funded volume arrangements are explained explicitly.

No site deployment, production bill, physical target, mainnet transaction or
manual wallet acceptance is established here. Final external acceptance stays in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
