# Prepaid fleet network costs (SF-083)

The person using the dApp pays the fleet's network costs up front: device gas
for receipts and data anchors, and Walrus storage for blobs. The design is an
on-chain **gas float** (user decision 2026-10-19). The owner deposits SUI
into a fleet budget contract. Registration sends each device a small capped
float, and devices top up only through the contract, within on-chain
limits. There is no server, gas station or sponsoring key. Walrus storage is
prepaid by the owner and held by a contract; the resulting blobs are owned
by the fleet.

Compromised devices are the primary threat. Every limit below bounds what a
fully compromised device key can spend, write or destroy.

## Contracts

### `sui_edge::fleet_budget` (core package, introduced by upgrade)

| Object | Holds |
|---|---|
| `FleetBudget` (shared) | the unallocated SUI balance, `paused`, default `Limits`, low-balance threshold, totals, the registry it serves and its owner cap's ID, a package `version`, and an index from device ID to its current `GasAccount` (dynamic fields) |
| `BudgetOwnerCap` (owned by the owner) | authority over that one budget |
| `GasAccount` (shared, one per enrolled device) | the device's own reserve (`Balance<SUI>`) it draws from, its limits, suspension, a `closed` flag, current period usage, last draw and totals, and the budget, device and owner cap it is bound to |

The budget also holds the fleet's cost settings record (`fleet_settings`,
SF-130) in a dynamic field; see [FLEET_SETTINGS.md](FLEET_SETTINGS.md).

`Limits` has these fields:
- `float_cap`: the most a single draw (and the initial float) may send;
- `period_ms`;
- `period_allowance`: the most drawn per period;
- `max_draws_per_period`;
- `min_draw_interval_ms`.

**Owner calls (owner cap only):**
- `create`, `deposit`, `withdraw` (unallocated funds);
- `set_paused`: stops every draw and upload at once;
- `configure`;
- `enroll_device`: needs the device's `DeviceOwnerCap`; shares the device's
  `GasAccount` with a reserve moved from the budget, sends the initial float
  (at most the float cap) and returns the account ID;
- `fund_reserve`, `reclaim_reserve`: move funds between the budget and a
  device's reserve;
- `fund_device`: an owner refill straight to the device's address, for a
  device with too little gas to draw;
- `set_device_limits`, `set_device_suspended`: write only the account and
  only read the budget;
- `remove_device`: closes the account for good, removes it from the index
  and returns its reserve to the budget;
- `migrate`.

**Device call:** `draw_gas(&budget, &mut account, registry, device_cap,
amount, clock)` sends `amount` from the device's own reserve to the sender.
It requires all of these:
- the current package version and an unpaused budget of this registry;
- the account is this device's, this budget's and the one the budget
  currently indexes for the device, and it is open and unsuspended;
- the device's signer-bound `DeviceCap` held by its current registry signer
  (`device_cap::assert_active`: custodied, unrevoked, current key);
- a registry identity still bound to the owner cap it was enrolled with;
- `amount ≤ float_cap`;
- the period's draw count and minimum interval;
- `drawn_in_period + amount ≤ period_allowance`;
- enough reserve.

A new period starts when the previous one has elapsed.

**Events:**
- `GasDrawn` for every draw, with the account, period usage, limits, the
  remaining reserve and `at_limit`;
- `DeviceFunded` for floats and refills;
- `ReserveChanged` for every allocation to or reclaim from a reserve;
- `Deposited`, `Withdrawn`;
- `BudgetLow` whenever the balance ends below the threshold;
- `DeviceStateChanged`, `BudgetPaused`, `BudgetConfigured`, `BudgetCreated`.

**What a compromised device can take.** SUI already sent to a device cannot
be clawed back. A compromised device holds at most its float plus what it
drew in the current period. Until the owner revokes its key, suspends it,
removes it or pauses the budget, it can draw at most one `period_allowance`
per period, in at most `max_draws_per_period` draws of at most `float_cap`,
and never more than its own reserve: the rest of the budget and other
devices' reserves are out of its reach. Revocation in the registry,
suspension, removal and the fleet-wide pause stop draws in the next
transaction. A device cannot withdraw, change limits, move reserves or touch
another device's account: those need the owner cap.

**Contention.** Draws do not write the fleet budget. `draw_gas` takes
`&FleetBudget` (pause flag, version and device index) and writes only the
drawing device's own `GasAccount`. Devices therefore never serialize on the
fleet object or on each other, and a device's draws never change the
budget's version: the isolated localnet run shows two devices drawing in
parallel against the same budget version, each writing only its own account,
with the budget listed as a read-only consensus input. The owner's
per-device changes (suspend, limits) also only read the budget. The pause
flag and the device index still bind immediately, because a writer of the
budget (pause, removal) is ordered before later readers.

Sui lets any address declare any shared object mutable in a transaction it
pays for. A compromised key can do that to the budget. It gets nothing more
than its limits allow, and the budget's contents stay unchanged (tested on
localnet). It only spends its own bounded gas, and Sui's per-object
congestion control prices such spam like any other. The daemon and the
signing service only ever submit the budget as an immutable input.

**Package versions.** Older Sui package versions stay callable. Every
mutating call checks `budget.version == VERSION`. A later upgrade that
changes draw rules bumps `VERSION` and the owner calls `migrate`, after which
the previous version's `draw_gas` aborts (`EWrongVersion`). The package that
predates this module has no `fleet_budget` at all (`FunctionNotFound`). Both
are tested on chain (below).

### `fleet_storage` (separate package)

`contracts/fleet_storage` depends on `sui_edge` and on Walrus. It is a
separate package so the core package keeps no Walrus dependency, and its
publish/upgrade pipeline and localnet tests stay unchanged.

**Pinned Walrus version.** `MystenLabs/walrus` tag `testnet-v1.59.0` (commit
`c68b530e`), `testnet-contracts/{walrus,wal}`. Its `Published.toml` names
the package that Testnet's `System` object reports today: `0x7d3b2086…`,
system version 4. An earlier pin, `testnet-v1.39.1`, linked version 2; its
calls would abort on Walrus's version check, so the dApp and the live run
check the live `System` package before using the pin. These were verified at
that version before building on them, and are exercised by the Move tests
against the real Walrus code:
- `Storage` and `Blob` have `key, store`, so the contract can hold them;
- `split_by_size`, `split_by_epoch` and `fuse` are public;
- `register_blob(&mut System, Storage, blob_id, root_hash, size, encoding, deletable, &mut Coin<WAL>)`
  requires storage of at least `encoding::encoded_blob_length` within its epochs;
- `certify_blob` verifies a storage-node quorum certificate;
- only a deletable blob can be deleted; `burn` needs the `Blob` by value;
- `extend_blob` pays from a WAL coin.

`FleetStorage` (shared, bound to one `FleetBudget`) holds the following.
Uploads also write Walrus's global `System` object, so they serialize on
Walrus itself whatever this package does.
- a single fused `Storage` pool: all deposits share an epoch range, or are
  adjacent with the same size;
- a WAL balance for write fees;
- per-device usage (dynamic fields, with quotas: encoded bytes, uploads,
  maximum blob size and write fees per period);
- the fleet's blobs (dynamic object fields) with a record of who uploaded
  each and when.

**Device upload.** `register_blob(fs, &budget, &account, …)` is authorized
like a draw (`fleet_budget::assert_may_draw` on the device's own account). It
works as follows:
- It computes the exact encoded size with Walrus's own `encoded_blob_length`
  and splits only that much storage from the pool.
- It offers at most the device's remaining write-fee quota from the WAL
  balance, returning the change.
- It registers the blob **non-deletable** and keeps it under the fleet
  object. No device call can delete, shorten or extract a fleet blob.
- `certify_blob` passes the storage nodes' certificate to Walrus, which
  verifies it. Certification cannot shorten or delete.

**Owner upload.** `register_fleet_blob` registers a blob the owner uploads
(a release artifact or recipe from the dApp) the same way: exact encoded
size from the pool, write fee from the fleet's WAL, non-deletable and held
by the fleet object, recorded without a device. It needs the budget's owner
cap.

**Owner calls:**
- `buy_storage`: buys storage from Walrus with the fleet's own WAL and adds it
  to the pool in one call, so no WAL leaves the fleet. It must fuse with the
  pool: an empty pool, the same epoch range, or adjacent with the same size.
- `refill_pool`: tops up the pool and keeps it until a given epoch, at any
  epoch, also after it expired. The dApp uses it for every purchase. Walrus
  joins storage only over the same epoch range, or adjacent ranges of the
  same size, so after the pool's first epoch `buy_storage` can no longer add
  to it. `refill_pool` first drops the pool's elapsed epochs, which no new
  blob can use, and an expired pool entirely; the owner receives the Sui
  storage rebate. It then extends the remaining unused bytes to the new end
  epoch at their size and buys the added bytes for the same range. The pool
  then covers [current epoch, end epoch). A refill cannot end before the
  pool does. Registered blobs keep their own storage and end epochs.
- `extend_pool`: pushes the whole pool's expiry back by buying the same size
  for the epochs right after it.
- `deposit_storage`, `withdraw_storage`, `deposit_wal`,
  `deposit_wal_at_least` (below), `withdraw_wal`.
- quotas.
- `extend_blob`, paid from the prepaid WAL.
- `release_blob`: hands the `Blob` to the owner, who can `burn` it after
  expiry and receives its Sui storage rebate.

Walrus storage itself is a consumed purchase, not a refundable deposit.

`fleet_storage` is built only against the pinned Walrus Testnet contracts.
No Mainnet build or verification exists, and the dApp says so.

## Device: daemon and signing service

`[budget]` in `config.toml` (see `config.example.toml`):

```toml
[budget]
budget_id = "0x<FleetBudget>"
low_water_mist = 20000000   # draw below 0.02 SUI; keep above one draw's gas
draw_mist = 50000000
```

It needs a manifest with the fleet budget type origins (an all-or-none
optional origin group, so older manifests keep working).

Each poll, the daemon reads its SUI balance (`GetBalance`). Below the
low-water mark, it reads the budget and, through the budget's device index,
its own `GasAccount`. It checks the account's type, that it is shared, and
that it names this budget and device. It then decides exactly as `draw_gas`
would, and draws only what the contract will accept, so a refused draw is
never submitted or charged. The draw is one checked call. Before signing,
the daemon verifies the simulated transaction's module, function and every
object and pure argument, and that the budget is an immutable input.

If the device cannot top up (paused, suspended, removed, unenrolled,
allowance or draw count used up, rate limited, reserve empty), the reason is journaled
(`funding.blocked`). Telemetry then reports `network funding: <reason>; N
receipt(s) queued until gas is available`. Status reports stay queued in the
journal across restarts, and nothing is dropped. They go out once a later
period or owner refill lets the device pay again. A device whose reserve is
empty waits for the owner to refill it ("Refill reserve" in the dApp). A
device with no gas at all cannot pay for its own draw, so the owner sends it
gas directly (`fund_device`, "Send float").

The SF-053 signing service signs draws only with `[budget]` in
`signer.toml` (`budget_id`, `max_draw_mist`). It requires all of these:
- exactly one `fleet_budget::draw_gas` on the manifest's package;
- that budget as an immutable input, a shared gas account (the contract
  binds it to this device and budget), the manifest's registry, this
  device's cap and the Clock;
- an amount of at most `max_draw_mist`;
- gas paid by the device within the gas ceiling;
- the daemon's main process as the caller.

Components and root get nothing. A compromised daemon remains bounded by
the on-chain limits. See [KEY_ISOLATION.md](KEY_ISOLATION.md).

**Device-originated Walrus data.** The daemon publishes encrypted logs,
diagnostics and shadow documents to prepaid Walrus storage through the reviewed
keyless codec and native registration/certification/anchor flow. Health summaries
are signed on chain, not shipped to a required HTTP server. Owner recipe/artifact
uploads use fleet storage from the dApp. See [serverless fleet data](SERVERLESS_FLEET_DATA.md)
and [product ownership/migration](SERVERLESS_PRODUCT.md); the earlier indexer-only
limitation has been superseded. Production provider/physical/Mainnet acceptance
remains separately tracked, not established by local fixtures.

## dApp

**Network budget page** (`/budget`):
- the estimate;
- optional card funding;
- creating the budget with the first deposit, and further deposits;
- balance, runway and warnings;
- per-device reserve and usage this period, with refill or reclaim of the
  reserve, sending a float, suspend/resume, remove, and enroll with float and
  reserve;
- fleet-wide pause/resume and withdrawal of unallocated funds;
- alerts for devices drawing at their limits or far above the fleet's
  typical device, for low or empty device reserves, and for low, empty or
  paused budgets;
- **Prepaid Walrus storage** (testnet builds with
  `NEXT_PUBLIC_FLEET_STORAGE_PACKAGE`):
  - creating fleet storage with default per-device quotas;
  - funding its WAL with SUI only (swap with a slippage limit, optionally
    refilling the pool in the same transaction), with WAL from the wallet, or
    by card;
  - refilling the pool with the fleet's WAL (`refill_pool`): add space and
    keep the pool for a number of epochs from now, at any epoch and after
    expiry, with the resulting size, epochs and cost at the live Walrus
    price shown first;
  - withdrawing unused WAL;
  - pool, expiry, WAL and blob totals, with alerts for no space, no WAL or an
    expiring or expired pool.

**Fleet settings page** (`/settings`, SF-131). The owner chooses the
settings that drive the fleet's cost and sees each option's cost before
saving:

- **Settings.** Update check, rollout fast-check, on-chain health, log
  archive cadence and log retention: SF-130's fields
  ([#265](https://github.com/tyler-suifleet/suifleet/issues/265),
  [FLEET_SETTINGS.md](FLEET_SETTINGS.md)), within the contract's bounds. The
  page starts from the fleet's saved settings when every saved value is one of
  the listed options, and shows the saved values either way. Retention is chosen in days and saved as whole Walrus
  epochs, rounded up: six weeks is 3 Mainnet epochs and 42 Testnet epochs. An
  option beyond the 53-epoch bound on the connected network (a year on
  Testnet) is disabled.
- **Each option's cost.** Every option shows its usage, its network cost per
  device and per fleet, and its SuiFleet price per device and per fleet.
  Update and rollout checks are chain reads with no network fee. Health
  reports and archives are device transactions, and retention is the Walrus
  storage the archives keep. The workload (fleet size, deployments, log data,
  releases and artifact size) is editable; the fleet size starts from the
  enrolled devices.
- **Network cost and price kept apart.** One panel shows the monthly network
  cost at cost, paid from the fleet budget. A separate panel shows the
  SuiFleet price: graduated base tiers, add-ons, the monthly minimum, the
  annual-prepay discount and the one-time activation. The two are never
  added together.
- **One price table** (`lib/fleet-pricing.ts`). Every default and price
  comes from the 2026-10-23 proposal in [BUSINESS_PLAN.md](BUSINESS_PLAN.md)
  (merged in [PR #268](https://github.com/tyler-suifleet/suifleet/pull/268)),
  which is not yet an approved price. A build can replace the whole table with
  `NEXT_PUBLIC_FLEET_PRICE_TABLE` (JSON of the same shape). A malformed table
  is refused whole and the page shows why, rather than partial prices.
- **Mainnet-rescaled estimator** (`lib/network-costs.ts`). The gas
  measurements were paid at Testnet's 1,000 MIST reference gas price.
  Mainnet's is 100. Storage gas is the same protocol constant (76) on both,
  so only the computation part of each measured fee is rescaled. Each
  measurement keeps its effects breakdown for this. The recorded Mainnet
  quote is a read-only snapshot,
  [docs/business/sf131/mainnet-quote.json](business/sf131/mainnet-quote.json)
  (`node scripts/quote_fleet_settings.mjs`; no transaction). A recurring
  health report uses the upper median of the recorded recurring reports, not
  their maximum. The maximum includes the summary object's one-time growth
  after the first report.
- **Live quotes, dated.** The page reads the connected network's reference
  gas price and Walrus storage and write prices (Mainnet or Testnet) over
  gRPC. USD spot prices come from CoinGecko only when the owner asks. Each
  part shows its source and date; whatever is not read live keeps the
  recorded Mainnet snapshot, labelled "recorded". The Network budget
  estimate uses the same quote, and its defaults now follow the default
  settings: daily health and four receipts a month, with log archives
  disabled as new fleets default. The Fleet settings page sizes archives
  from the chosen cadence and retention.
- **Deposit sized from the settings.** The recommended SUI and WAL deposit
  covers the chosen settings, fleet size and prepaid months. Per-month counts
  round up to whole reports and archives, with 20% headroom. It is compared
  with the budget balance and, when the deployment has fleet storage, the
  storage's WAL.
- **Warning before signing.** Saving with too little deposit shows a warning
  first, with the shortfall and a link to deposit; nothing is signed. "Save
  anyway" then signs only the chosen settings. Changing a choice withdraws
  the warning, so it is checked again.
- **Saving on chain.** Save signs one SF-130 `fleet_settings::set` call
  with the fleet budget, its owner cap, the clock and the chosen values
  (`lib/fleet-settings-tx.ts`, via `buildSetFleetSettingsTx`). It is offered
  only when the deployment manifest records the settings type origins and
  the wallet holds a budget's owner cap; otherwise the page says why, the
  Save button stays disabled and prices and the deposit still work. A wallet
  refusal or failed transaction reports that nothing was saved.

Until deposit reclaim
([SF-129/#264](https://github.com/tyler-suifleet/suifleet/issues/264)), a
weekly archive's Walrus storage deposit dominates the default network cost.
That is about $0.10 per device a month at the recorded quote, above the
proposed $0.08 first-tier base price, as the business plan notes.

**Fleet-paid uploads** (`lib/fleet-upload.ts`). On the Releases page, the
Components publisher and the Greengrass import, "Pay Walrus storage from the
fleet's prepaid storage" sends the (Seal-encrypted or explicitly public)
bytes through fleet storage instead of the publisher. There is no server and
no fallback:
1. the browser encodes the blob with the pinned Walrus SDK
   (`@mysten/walrus` 1.2.35, WASM) after checking that the live Walrus
   `System` package is the pinned one;
2. the wallet signs `register_fleet_blob`, and the blob object comes from the
   confirmed effects' `FleetUploadRegistered` event for this storage and blob;
3. the slivers go straight to the storage nodes;
4. the wallet signs `certify_blob` with the nodes' certificate.

The blob is fleet-owned and non-deletable, and renews only through fleet
storage. A failure before certification reports the error and leaves an
uncertified fleet blob the owner can release.

**First run.** SF-088's extension point, between the gas check and
registration, now shows these steps:
1. the estimate;
2. card funding;
3. the deposit;
4. registration, which enrolls the device with its float and its own gas
   account and reserve in the same PTB.

The page still checks the network and account, and explains that wallet gas
and the fleet budget differ.

**Estimate** (`lib/network-costs.ts`). It uses the SF-054 measurements:
- the applied-receipt net gas (1,251,568 MIST);
- Walrus Mainnet rates (57,321 FROST per MiB-epoch, 104,135 FROST per MiB
  written);
- the on-chain RS2 encoded length, which reproduces SF-054's exact sizes;
- the recorded spot quotes.

From those it lists device gas (receipts, anchors and draws), the initial
floats, a SUI deposit with headroom, and WAL for release and device-data
storage. Separate rows show the swap route, card and on-ramp fees, regional
availability and the (absent) service fee before the user approves. It is an
estimate: the wallet shows each transaction's real cost.

**SUI-only deposits.** On Testnet, the storage deposit swaps SUI for WAL
through Walrus's pinned Testnet exchange (`wal_exchange::exchange_all_for_wal`,
fixed posted rate) in the same PTB. It then calls
`fleet_storage::deposit_wal_at_least`, which aborts the whole transaction if
fewer WAL than the minimum arrive, and can call `refill_pool` with that WAL
in the same transaction. The dApp reads the exchange's posted rate and WAL
reserve first and sets the minimum from the user's slippage limit. No DEX
route is pinned and verified for Mainnet, so the dApp explains that WAL must
be bought directly there.

**Card payment without a server** (`lib/onramp.ts`).
- The dApp opens OnRamp.money's own hosted checkout (`https://onramp.money/main/buy/`)
  in a new tab. It is pre-filled with the wallet address, asset (`sui` or
  `wal`), network `sui` and amount, using only a public numeric app ID
  (`NEXT_PUBLIC_ONRAMP_APP_ID`).
- KYC, payment, fees and country availability stay with the provider.
  SuiFleet uses no webhook and no provider API.
- Coinbase Onramp is not offered. Its hosted flow now needs a session token
  minted server-side with a secret CDP key.
- The parameter names follow OnRamp.money's published SDK configuration. A
  real checkout is final external acceptance (SF-087).

**Failed or uncertain purchases.** A purchase counts only when the wallet
balance rises, and nothing is credited to the budget until the user's own
deposit transaction confirms. A purchase not seen within two hours is shown
as "not confirmed": it failed, was cancelled or is still with the provider.
This is shown separately from budget exhaustion. A failed purchase never
changes existing Sui state, prepaid storage or installed applications.

## Read interface for SF-092 (version 1)

SF-092 builds reserves, renewal and out-of-browser notification on these
reads without changing this authority model:

| Read | Source |
|---|---|
| Interface version | `fleet_budget::interface_version()` = 1; `fleet_storage::interface_version()` = 1 |
| Balance, spendable, reserved | `balance` (unallocated), `spendable` (= balance), `reserved` (= 0 until SF-092 adds renewal reserves; device gas reserves are per account, below) |
| Budget state | `paused`, `default_limits`, `low_balance_threshold`, `device_count`, `total_deposited/withdrawn/paid/allocated/reclaimed`, `version` |
| Per-device float and quota | `account_id(budget, device_id)` → the shared `GasAccount` and its getters (`account_reserve`, limits, usage, `account_closed`); `fleet_storage::usage(storage, device_id)` |
| Storage and expiry | `pool_size`, `pool_epochs` (`[start, end)`: blobs end with the pool), `wal_balance`, `blob_record` (end epoch per blob; device none for owner uploads) |
| Events | `GasDrawn` (`at_limit`, `reserve`), `DeviceFunded`, `ReserveChanged`, `Deposited`, `Withdrawn`, `BudgetLow` (exhaustion), `DeviceStateChanged`, `BudgetPaused`, `UploadRegistered` (`at_limit`), `FleetUploadRegistered`, `StoragePurchased`, `StorageDeposited`, `WalDeposited`, `BlobExtended`, `BlobReleased` |

The TypeScript reads are in `dapp/src/lib/fleet-budget.ts` (`fetchBudgets`,
`fetchAccount`, `fetchDraws`, `fetchStorage`). Layouts are in the reviewed ABI
(`interfaces/fleet-v1.json`, optional origin group `fleet_budget`).

## Business decisions and future options

- **Service fee: open business decision.** The deposit covers network costs
  only. No SuiFleet fee is charged unless the user decides otherwise
  ([BUSINESS_PLAN.md](BUSINESS_PLAN.md)).
- **Future option: sponsored transactions through a gas station.** This needs
  a live sponsor signature from an always-on signer. It was rejected for now.
- **Future option: Nautilus-enclave sponsorship.** Enclaves are
  self-managed AWS instances. Rejected for now.

## Verification

```bash
sui move test --path contracts                    # 168 tests, 44 fleet_budget
sui move test --path contracts/fleet_storage      # 39 tests against Walrus testnet-v1.59.0 (in make test-contracts)
make edge-test                                    # funding, exact-draw check (immutable budget), signer draw policy
python3 scripts/test_budget_ota.py                # in make test-ota: real daemon vs simulator
node scripts/test_fleet_budget.cjs                # in make test-manifest: estimate vs SF-054, on-ramp, builders, alerts, fleet-paid upload flow
node scripts/test_fleet_settings_cost.cjs         # in make test-manifest: Mainnet rescaling vs quote snapshot, price table, per-setting cost, deposit, live quotes
make test-fleet-settings                          # in make ci: the above, SF-130's test_fleet_settings.cjs and the real-browser settings page (scripts/test_fleet_settings_browser.cjs)
make test-budget-localnet                         # network none: upgrade, read-only budget, parallel draws, refusals, version gate, daemon draws
make test-budget-testnet SUIFLEET_WALLET=…        # public testnet, dedicated agent wallet, lock required
```

- **Move tests.** Limits, period resets, revocation, rotation, deregistration,
  pause, suspension, removal, ownership and cap checks, a compromised key
  draining at the maximum rate and bounded by its reserve, draws leaving the
  budget unchanged, separate accounts (another device's account, another
  budget's account, a retired account after re-enrollment), reserve funding
  and reclaim, and the version gate on the budget and its accounts. For
  storage: exact storage splitting, non-deletable fleet ownership for device
  and owner uploads, quotas, write-fee caps, slippage, purchases and pool
  extension with fleet WAL, release/extend and version. Refills of a partly
  used pool in its first epoch and after it (elapsed epochs dropped, unused
  bytes kept and extended, WAL cost equal to Walrus's price for exactly the
  added storage), of an expired pool, and refusals to shorten the pool, to
  end in the past, to leave no storage and from another cap. The Walrus
  test system cannot advance its epoch from this package, so the
  later-epoch cases run the same code through a test-only entry point that
  takes the epoch.
- **Isolated localnet** ([sf083-localnet.json](validation/sf083-localnet.json)).
  A base (main) package with an activated device is upgraded to head. The
  pre-upgrade device enrolls and draws from its own account; the budget is a
  read-only consensus input at an unchanged version. Two more devices draw
  in parallel: both read the same budget version and each writes only its own
  account. These refusals were recorded as failed transactions on chain:
  - the base package: `FunctionNotFound`;
  - above the float cap: 8;
  - revoked: `device_cap` 2;
  - paused: 3;
  - suspended: 7;
  - over the allowance: 9;
  - over the draw count: 10;
  - an emptied reserve, and beyond the reserve with the budget declared
    mutable: 11;
  - another device's account: 16;
  - a removed device's account: 4;
  - the previous version after a later upgrade's migration: 0.

  A draw that declares the budget mutable succeeds within its limits and
  leaves the budget's contents unchanged. The device's attempt to use the
  owner cap is refused before execution. The real daemon draws three times
  over gRPC with the budget read-only and then journals its limit.
- **Public testnet** ([sf083-testnet.json](validation/sf083-testnet.json),
  2026-10-21, head `011ee1c`). The run used the dedicated agent wallet, chain
  `4c78adac`, held `network:suifleet-testnet`, and made one faucet request
  through the pinned 1.82.1 CLI. Every transaction's effects were checked.
  - **Publication.** The head `sui_edge` and `fleet_storage` packages were
    published with bytecode verified before signing (`0x34978350…`,
    `0x92b60591…`) and cached by content hash. Runs 1 and 2 stopped on harness
    assertions (an owner rendering and an effects format), not on contract
    behavior. Run 3 reused the fixture and passed every check. The budgets
    left by runs 1 and 2 were reclaimed with the owner cap.
  - **Budget.** A budget was created with a 0.06 SUI deposit. A throwaway
    device was enrolled: its address received exactly its 0.02 SUI float, and
    its own shared `GasAccount` holds a 0.025 SUI reserve. It drew 0.005 SUI
    from that account (`GasDrawn`, net gas 1,988,540 MIST). The fleet budget
    stayed at the same object version and was not among the mutated objects.
  - **On-chain refusals:** above the float cap (8), after revocation
    (`device_cap` 2) and while paused (3).
  - **Real daemon.** It drew twice from its account over the public fullnode,
    then journaled its period draw limit.
  - **SUI-only storage funding.** 0.02 SUI was swapped for WAL through the
    pinned exchange into fleet storage in one PTB. A minimum above the posted
    rate aborted (12).
  - **Storage bought with the fleet's WAL.** `buy_storage` bought 132,068,000
    encoded bytes for epochs 558–561 for 986,580 FROST, and `extend_pool`
    extended the pool to epoch 562. No WAL left the fleet object.
  - **Device upload.** The device registered and certified a 2 KiB blob
    through its account: exactly its encoded size, from fleet storage,
    non-deletable, owned by the fleet object, confirmed by 69 storage nodes,
    read back intact and ending with the extended pool. Its second upload in
    the period aborted on its quota (6).
  - **dApp owner upload.** The dApp's own `uploadToFleetStorage`
    (`scripts/live_fleet_upload.cjs`, the app's pinned `@mysten/sui` 2.36.0
    and `@mysten/walrus` 1.2.35) registered and certified an owner blob from
    fleet storage, with a 328,860 FROST write fee from the fleet's WAL. Each
    transaction was decoded and checked (sender, gas owner, gas budget,
    exactly one expected `fleet_storage` call) before the wallet signed it.
    The blob is fleet-owned and non-deletable, was read back intact, and used
    the pool's last space.
  - **Cleanup and cost.** The device was removed and its remaining reserve
    returned. The unused deposit was withdrawn and the device key swept. The
    wallet's net decrease was 0.540 SUI: about 0.48 SUI for the one-time
    package fixture (package storage), 0.066 SUI for the passing run, and the
    two failed runs' setup, whose budgets were reclaimed.

The earlier shared-budget design's runs (head `ada17bc`) are kept under
`superseded` in the evidence file.

`refill_pool` was added after this run. It has Move tests only; no public
testnet run has used it yet, and a live refill after the pool's first Walrus
epoch needs a pool at least one Walrus epoch (one Testnet day) old.

## Outstanding acceptance

The settings page's browser test signs through a controlled wallet that
decodes the real `fleet_settings::set` transaction; SF-130's localnet test
executes that call on chain. Signing it from a real wallet session is final
external acceptance. Real card checkout and real wallet sessions
are deferred to final external acceptance in [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159)
and [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32). So
are Mainnet deposits, swaps and uploads, and physical devices. None of them
has run.

## SF-092 retention extension

Storage mutation/read interface v2 and the SF-092 prepaid-capacity extension
are documented in [RETENTION_RENEWAL.md](RETENTION_RENEWAL.md). The v1 read table
above records the original SF-083 boundary. Core budget interface v1 is unchanged;
renewal WAL/SUI reserves are separate from discretionary WAL and device accounts.
Actual bounded testnet renewal, certification/readback and original-intent restart
evidence is recorded in [the SF-092 receipt](validation/sf092-testnet.json).
This does not establish final manual-wallet, production-provider, mainnet or
physical acceptance; exact-head CI and independent review remain separate gates.

## Batch enrollment (SF-132)

On the Network budget page, **Enroll all unenrolled devices** enrolls activated
owner-held devices in sequential PTBs of at most 200 calls. Every chunk builds
and simulates before wallet approval, checks the resolved transaction is at most
120,000 bytes and its estimated gas budget at most 2 SUI, and uses the normal
manifest/account/network guards and independently confirmed execution effects.
The wallet pays transaction gas separately from the budget's device funding.

Progress shows confirmed devices, already-enrolled devices, the current approval
and confirmed digests. Rejection, failed effects, read/preflight errors and
uncertain confirmation stop subsequent chunks. **Resume enrollment**, including
after reload, checks the chain's budget index and accounts again. It never trusts
a local completion cache. A still-unindexed submission can temporarily cause an
atomic duplicate-enrollment abort; wait for indexing before retrying. Previously
confirmed chunks remain enrolled. Reads and transaction authorization failures
never count as successful enrollment.

The initial scan excludes existing accounts from funding requirements. Each chunk
refreshes the owner's budget and reserves funding for every outstanding initial
float, then allocates up to two period allowances per device (bounded by the
contract's 1,000 SUI reserve ceiling). If funds are limited, the reserve available
beyond the floats is distributed evenly. Insufficient funds for the floats stop
the run before requesting approval; deposit and resume.

Enrollment status and eligibility use deterministic dynamic-field IDs and batches
of at most 50 objects per request, with one request outstanding at a time. The
offline 1,000-device request-count regression exercises the real orchestration
and readers: **120 batched account/registry reads**, **72 before approval one**,
including the full scan and every per-chunk eligibility recheck. At the browser's
350 ms queue spacing these reads account for about **42 seconds total** and
**25.2 seconds before the first approval**, rather than 35 and 21 minutes.
These are calculated queue contributions, not measured wall-clock completion:
network latency, budget refreshes, dashboard polling, transaction build/simulation
and wallet interaction add time. Existing-account resumes also batch the account
objects; malformed fields and transport failures stop the run rather than being
reported as unenrolled or ineligible devices.

The [isolated localnet measurement](validation/sf132-localnet.json) enrolled
1,000 devices in **five** 200-device transactions: 36,036 bytes each, estimated
gas budget 1,523,193,571 MIST and actual net gas 1,522,193,670 MIST per chunk
(computation + storage − rebate). The total enrollment gas was 7.61096835 SUI;
this excludes registration and the device floats/reserves. These are localnet
measurements with 1,000 MIST float and 4,000 MIST reserve per device, not public
network quotes. The run rejected the second approval after 200 confirmed,
resumed the other 800, verified all accounts and amounts, then reran with zero
additional approvals. No Move API, layouts, dependencies or migration changed.

Reproduce in a disposable network-none dev-image container with installed locked
frontend dependencies, cached Move dependencies and the checksum-pinned network
CLI at `/opt/sui-network/sui`:

```bash
node scripts/test_budget_enrollment.cjs
# Inside the disposable network-none container:
BATCH_EVIDENCE=/tmp/sf132-localnet.json make test-budget-enrollment-localnet
```

Frontend typecheck/build and the Wallet Standard browser fixture also pass;
the fixture checks connect/sign/rejection/effects/network errors and disconnect.
Manual extension-wallet/production-provider, physical and mainnet acceptance
remain unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
