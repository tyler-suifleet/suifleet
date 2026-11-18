# Hosted subscription card funding — SF-183

Checked 2026-11-04 against the official sources below. This is preliminary
provider diligence, not regulator verification or a provider recommendation.
**No production provider is selected, partner account created or checkout enabled.**
The user reserves provider choice and signup. The provider-independent Testnet
implementation is separately reviewable under the explicit recovery instruction.
Full [SF-183/#372](https://github.com/tyler-suifleet/suifleet/issues/372) acceptance
remains incomplete; production/card acceptance remains in
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159) and manual
wallet acceptance in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).

| Question | OnRamp.money | Banxa |
|---|---|---|
| Operating entity | The earlier intake's [terms endpoint](https://onramp.money/static/termsAndConditions.pdf) identified OMO LLC (Saint Vincent and the Grenadines). On this check the endpoint returned no readable terms: reconfirm the contracting entity before selection. The current [compliance statement](https://onramp.money/assets/compliance.pdf), effective June 1, 2026, names Buyhatke Internet Private Limited in India and FINSTAR AI Sp. z.o.o in the EU. | Jurisdiction-specific entities. [US statement](https://support.banxa.com/en/support/solutions/articles/44002771746-does-banxa-have-money-transmitter-licences-us-) names BNXA USA MTL Inc.; [global licenses](https://banxa.com/Global-License), dated September 21, 2026, names EU Internet Ventures B.V. for the Netherlands. The actual customer contract must identify the applicable entity. |
| US licensing | US state permissions were **not verified**. FIU-IND registration does not establish US licensing. | The [US license document](https://banxa.com/USA-licenses) lists state-specific permissions. The intended customers' states and current regulator records still require confirmation; this is not an all-US eligibility claim. |
| EU licensing | A named EU affiliate is not proof of MiCA authorization. The statement does not establish verified EU-wide authorization; **unverified**. The earlier intake also named OME Ventures; the current statement does not list it. | The [MiCA statement](https://support.banxa.com/en/support/solutions/articles/44002771745-does-banxa-have-a-mica-licence-) reports Dutch authorization in October 2025 and EEA coverage; the global document lists AFM CASP #41000017. Independent regulator-record/partner-scope verification remains outstanding. |
| Hosted KYC | The compliance statement describes customer due diligence, sanctions screening and transaction monitoring. The [site](https://onramp.money/) describes provider-hosted identity checks. SuiFleet should receive no identity documents. | The [KYC framework](https://docs.banxa.com/reference/global-kyc-framework) specifies dynamic tiers by amount, payment method and jurisdiction, including document/liveness checks where required. Keep checks in hosted checkout; do not implement partner KYC sharing. |
| Regions | The site advertises 75+ countries but also describes narrower payment-method coverage. Actual US/EU customer, card, partner and asset eligibility is not established by these marketing claims. | The [country list](https://support.banxa.com/en/support/solutions/articles/44002216505-what-countries-are-supported-by-banxa-) and KYC framework describe regional coverage and US state exclusions. Eligibility varies by jurisdiction, asset and method; recheck for the actual customer. |
| Fees | No universally applicable card fee or final net-USDC amount was verified. The hosted quote must disclose processing fees, spread, network fees, limits and final delivery before the customer pays. | [EUIV pricing](https://support.banxa.com/en/support/solutions/articles/44002625876-how-does-eu-internet-ventures-b-v-euiv-set-the-price-of-cryptocurrency-) lists a 1.99% card processing fee, network charges and variable spreads. This is a regional published example, not a SuiFleet/card quote or a universal rate. Confirm final net-USDC and bank charges. |
| USDC on Sui | Listed by [Sui's asset table](https://docs.sui.io/onchain-finance/asset-custody/fiat-on-ramps). Confirm current partner-specific native coin type, regions and card delivery; no bridged USDC substitution. | Also listed by Sui. Recheck [supported assets](https://docs.banxa.com/reference/supported-cryptocurrencies-and-blockchains) and actual partner/region coverage. |
| Serverless candidate | Existing SF-083 hosted URLs use a public numeric app ID for network-budget SUI/WAL. This does not authorize subscription USDC. No USDC provider URL contract is implemented or inferred. | [Referral integration](https://docs.banxa.com/products/hosted-checkout/docs/referral-integration/constructing-referral-urls) documents a partner subdomain and public URL parameters. Signup supplies that subdomain. API order creation is a different, authenticated integration and must not be placed in the browser. |

Before enabling either adapter, the user must choose it and complete signup;
then verify the partner's current public URL parameters, native USDC-on-Sui
support, exact recipient handling, net purchase amount and separate SUI gas
funding. Do not insert a secret, webhook, server, guessed partner ID, provider
session contract or arbitrary checkout URL. A provider callback cannot prove a
purchase or subscription payment. Production configuration must be explicit and
separate from the existing network-budget app ID.

The intended production flow remains: hosted card purchase into the customer's
wallet, verified-chain balance observation, fresh offer/administrator checks,
explicit wallet prepay through SF-182, then typed effects/event confirmation.
USDC cannot pay SUI gas by itself. No gas top-up, sponsorship or provider promise
is inferred from USDC arrival. Real-card, provider-account and final human wallet
checks are unrun. User choice and adapter implementation are unresolved scope,
not a claimed success or a reason to block this authorized partial software PR.
