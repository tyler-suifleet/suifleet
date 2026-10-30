# Prepaid crypto subscription accounts (SF-181)

The 2026-10-27 payment decision is crypto plus card, without a SuiFleet billing
server. This module implements the crypto account. Card settlement and checkout
are separate implementations; this change does not claim they exist.

Registry authority publishes an immutable `Offer<T>` with a treasury, plan tier,
device capacity, allowance and a coin-unit price. USDC is primary; SUI may be
explicitly enabled. A reviewed USD quote supplies that integer conversion: no
mutable oracle, automatic overage invoice or mid-term price adjustment exists.
Monthly terms last 30 days. Annual prepay lasts 365 days in twelve equal periods,
with 10% off each monthly fee, rounded upward to a coin's smallest unit.
The full term must be deposited before opening or renewing. Surplus deposits
stay refundable. Offers expire 60 days after publication; an old offer cannot
be reused forever to evade renewal repricing. Paid accounts retain their original
terms even after their offer expires.

`open` checks customer administration and registry binding, then seals exactly
one persistent `Account<T>` to that Customer. Cancellation does not remove its
binding or permit a second account in another coin. Changing coins requires a
separate reviewed migration. The treasury fixed at opening cannot be changed by
renewal, an administrator or a refund caller.

`collect` is permissionless and pays exactly one started period at a time. The
first period starts at opening; later ones start at their recorded boundary.
Repeated early calls abort. Late callers can catch up only as far as the paid
term, without resetting the same period twice. The Customer's existing monotonic
quota ledger is advanced by the account-bound package authority; changing wallets
cannot reset it. Someone must submit the transaction: Move does not run timers.
This change adds no billing signer, always-on billing service or background worker.
It does not claim daemon scheduling or automatic service shutdown at expiry.

Cancellation is authorized by a current customer administrator. It settles the
fixed fees for every period that has already started, including periods whose
collection has not yet been submitted, and returns the entire remaining balance
as a `Coin<T>` to that administrator's transaction. The transaction must transfer
that coin to the customer's selected account. A started period is nonrefundable;
unstarted periods and surplus are refundable in full, without a cancellation fee
or annual-discount clawback. Repeated cancellation cannot charge the treasury
again; deposits made after cancellation can still be refunded. Registry authority
has no withdrawal, sweep, fee mutation or refund-recipient override.

Renewal requires explicit customer approval after the old term ends and all its
periods are collected or cancellation has settled them. It applies a new immutable
offer, published at least 30 days before acceptance, without changing the payee.
The planner's 3× SUI-cost trigger remains an operator quoting rule, not permission
to change an account's terms. This is software enforcement of notice and fixed
terms, not approval of a production tariff or legal contract.

## Upgrade and client boundary

The existing Customer layout and type origin stay unchanged. Subscription opt-in
sets its authority version to 2; old package credential, entitlement and managed
fleet entry points require version 1 and reject it. The current package supports
both versions for customer operations, but manual `set_entitlement` still accepts
only version 1. Only the sealed subscription account advances a version-2 paid
entitlement. Existing customers are not automatically migrated. `SubscriptionKey`,
`Offer`, `Account` and events have their introduction-upgrade origins in the new
all-or-none manifest origin group `subscription`.

Payment callers must verify the deployment first, obtain the recipient solely
from `treasuryPayee(expectedPayee)`, and check the offer/account's exact coin type
against `subscriptionCoin(symbol, expectedType, expectedPayee)`. A manifest with
`subscription_coins` requires a pinned treasury and USDC, optionally SUI. Coin
origin addresses are full lowercase 32-byte addresses. Decimals are six for USDC
and nine for SUI. Public profiles require Circle's canonical USDC type:

- Testnet: `0xa1ec7fc00a6f40db9693ad1415d0c193ad3906494428cf252621037bd7117e29::usdc::USDC`.
- Mainnet: `0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC`.

These are [Circle's documented origins](https://developers.circle.com/stablecoins/usdc-contract-addresses).
A symbol is never coin authority. No mainnet manifest, publication or payment is
configured here. The testnet treasury remains the reviewed SF-185 descriptor in
`deployments/testnet/treasury.json`.

## Payment Kit evaluation

[Mysten Payment Kit](https://sdk.mystenlabs.com/payment-kit) supports arbitrary
coins, payment receipts and duplicate prevention, but has experimental APIs and
no recurring subscription collection. Its direct receiver payment is not this
account's escrow deposit. A `sui:pay` link that transfers to the treasury would
bypass prepaid custody and refunds. Use a PTB calling `open` or `deposit` instead;
transaction digests and typed account events identify deposits. No Payment Kit
package, package ID or SDK dependency is adopted or silently resolved by network.
Payment links must eventually open the verified checkout that builds this PTB;
this contract change does not advertise ordinary transfer links as subscriptions.

## Checks and remaining acceptance

```bash
make test-contracts
make generate-interfaces
make check-interfaces
python3 scripts/check_move_security.py
make test-subscription-localnet
```

The last command requires the pinned dev image with network disabled, cached Move
dependencies and locked frontend dependencies. It publishes the merged current
base and performs a genuine compatible upgrade over an existing Customer before
checking collection, refund state and rejected old-package entry points. Its SUI
fixture is not evidence of Circle USDC on public testnet.

The dedicated agent had no canonical testnet USDC during the initial read-only
preflight. Circle's public Sui faucet requires reCAPTCHA. The required public USDC
deposit/collect/cancel effects and gas remain unrun until that wallet is funded;
no substitute token counts as USDC acceptance. The ticket must retain that gate.
Physical, mainnet, production-provider and manual-wallet acceptance remain unrun
in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
