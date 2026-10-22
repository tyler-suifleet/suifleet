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
| `FleetBudget` (shared) | the SUI balance, `paused`, default `Limits`, low-balance threshold, totals, the registry it serves and its owner cap's ID, and a package `version` |
| `BudgetOwnerCap` (owned by the owner) | authority over that one budget |
| `DeviceAccount` (dynamic field, keyed by device ID) | the device's limits, suspension, current period usage, last draw and totals |

`Limits` has these fields:
- `float_cap`: the most a single draw (and the initial float) may send;
- `period_ms`;
- `period_allowance`: the most drawn per period;
- `max_draws_per_period`;
- `min_draw_interval_ms`.

**Owner calls (owner cap only):**
- `create`, `deposit`, `withdraw`;
- `set_paused`: stops every draw and upload at once;
- `configure`;
- `enroll_device`: needs the device's `DeviceOwnerCap`; sends the initial float, at most the float cap;
- `fund_device`: an owner refill, for a device with too little gas to draw;
- `set_device_limits`, `set_device_suspended`, `remove_device`;
- `migrate`.

**Device call:** `draw_gas(budget, registry, device_cap, amount, clock)` sends
`amount` to the sender. It requires all of these:
- the current package version and an unpaused budget of this registry;
- an enrolled, unsuspended account;
- the device's signer-bound `DeviceCap` held by its current registry signer
  (`device_cap::assert_active`: custodied, unrevoked, current key);
- a registry identity still bound to the owner cap it was enrolled with;
- `amount ≤ float_cap`;
- the period's draw count and minimum interval;
- `drawn_in_period + amount ≤ period_allowance`;
- enough balance.

A new period starts when the previous one has elapsed.

**Events:**
- `GasDrawn` for every draw, with period usage, limits, the remaining
  balance and `at_limit`;
- `DeviceFunded` for floats and refills;
- `Deposited`, `Withdrawn`;
- `BudgetLow` whenever the balance ends below the threshold;
- `DeviceStateChanged`, `BudgetPaused`, `BudgetConfigured`, `BudgetCreated`.

**What a compromised device can take.** SUI already sent to a device cannot
be clawed back. A compromised device holds at most its float plus what it
drew in the current period. Until the owner revokes its key, suspends it,
removes it or pauses the budget, it can draw at most one `period_allowance`
per period, in at most `max_draws_per_period` draws of at most `float_cap`.
Revocation in the registry, suspension and the fleet-wide pause stop draws
in the next transaction. A device cannot withdraw, change limits or touch
another device's account: those need the owner cap.

**Contention.** Per-device state lives in dynamic fields, so the budget
object itself stays small. Each draw still writes the shared budget, which
holds the pooled balance. Draws are rare (only below the low-water mark) and
rate limited. Every attempt, including an aborted one, costs the sender at
least one computation fee from its own bounded float. Pausing or suspending
ends a flood.

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

`FleetStorage` (shared, bound to one `FleetBudget`) holds:
- a single fused `Storage` pool: all deposits share an epoch range, or are
  adjacent with the same size;
- a WAL balance for write fees;
- per-device usage (dynamic fields, with quotas: encoded bytes, uploads,
  maximum blob size and write fees per period);
- the fleet's blobs (dynamic object fields) with a record of who uploaded
  each and when.

**Device upload.** `register_blob` is authorized like a draw
(`fleet_budget::assert_may_draw`). It works as follows:
- It computes the exact encoded size with Walrus's own `encoded_blob_length`
  and splits only that much storage from the pool.
- It offers at most the device's remaining write-fee quota from the WAL
  balance, returning the change.
- It registers the blob **non-deletable** and keeps it under the fleet
  object. No device call can delete, shorten or extract a fleet blob.
- `certify_blob` passes the storage nodes' certificate to Walrus, which
  verifies it. Certification cannot shorten or delete.

**Owner calls:** `deposit_storage`, `withdraw_storage`, `deposit_wal`,
`deposit_wal_at_least` (below), `withdraw_wal`, quotas, `extend_blob` (from
the prepaid WAL) and `release_blob`. Release hands the `Blob` to the owner,
who can `burn` it after expiry and receives its Sui storage rebate. Walrus
storage itself is a consumed purchase, not a refundable deposit.

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
low-water mark, it reads the budget and its `DeviceAccount` and decides
exactly as `draw_gas` would. It draws only what the contract will accept, so
a refused draw is never submitted or charged. The draw is one checked call:
the daemon verifies the simulated transaction's module, function and every
object and pure argument before signing.

If the device cannot top up (paused, suspended, unenrolled, allowance or
draw count used up, rate limited, budget empty), the reason is journaled
(`funding.blocked`). Telemetry then reports `network funding: <reason>; N
receipt(s) queued until gas is available`. Status reports stay queued in the
journal across restarts, and nothing is dropped. They go out once a later
period, deposit or owner refill lets the device pay again. A device with no
gas at all cannot pay for its own draw. The owner refills it
(`fund_device`, "Refill float" in the dApp).

The SF-053 signing service signs draws only with `[budget]` in
`signer.toml` (`budget_id`, `max_draw_mist`). It requires all of these:
- exactly one `fleet_budget::draw_gas` on the manifest's package;
- that budget, the manifest's registry, this device's cap and the Clock;
- an amount of at most `max_draw_mist`;
- gas paid by the device within the gas ceiling;
- the daemon's main process as the caller.

Components and root get nothing. A compromised daemon remains bounded by
the on-chain limits. See [KEY_ISOLATION.md](KEY_ISOLATION.md).

**Device-originated Walrus data.** The contract, the device-key flow and a
real Testnet upload are implemented and tested (below). The daemon itself
has no Walrus write path today, since telemetry and logs go to the fleet
indexer. Archiving device data to Walrus through `fleet_storage` from the
daemon needs an RS2 encoder in the device client. That is follow-up work,
reported in #161.

## dApp

**Network budget page** (`/budget`):
- the estimate;
- optional card funding;
- creating the budget with the first deposit, and further deposits;
- balance, runway and warnings;
- per-device usage this period, with refill, suspend/resume, remove, and
  enroll with float;
- fleet-wide pause/resume and withdrawal of unused funds;
- alerts for devices drawing at their limits or far above the fleet's
  typical device, and for low, empty or paused budgets.

**First run.** SF-088's extension point, between the gas check and
registration, now shows these steps:
1. the estimate;
2. card funding;
3. the deposit;
4. registration, which enrolls the device with its float in the same PTB.

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

**SUI-only deposits.** On Testnet, the deposit swaps SUI for WAL through
Walrus's pinned Testnet exchange (`wal_exchange::exchange_all_for_wal`, fixed
posted rate) in the same PTB. It then calls
`fleet_storage::deposit_wal_at_least`, which aborts the whole transaction if
fewer WAL than the minimum arrive. No DEX route is pinned and verified for
Mainnet, so the dApp explains that WAL must be bought directly there.

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
| Balance, spendable, reserved | `balance`, `spendable` (= balance), `reserved` (= 0 until SF-092 adds reserves) |
| Budget state | `paused`, `default_limits`, `low_balance_threshold`, `device_count`, `total_deposited/withdrawn/drawn`, `version` |
| Per-device float and quota | `device_account(budget, device_id)` → `DeviceAccount` getters; `fleet_storage::usage(storage, device_id)` |
| Storage and expiry | `pool_size`, `pool_epochs` (`[start, end)`: blobs end with the pool), `wal_balance`, `blob_record` (end epoch per blob) |
| Events | `GasDrawn` (`at_limit`), `DeviceFunded`, `Deposited`, `Withdrawn`, `BudgetLow` (exhaustion), `DeviceStateChanged`, `BudgetPaused`, `UploadRegistered` (`at_limit`), `StorageDeposited`, `WalDeposited`, `BlobExtended`, `BlobReleased` |

The TypeScript reads are in `dapp/src/lib/fleet-budget.ts` (`fetchBudgets`,
`fetchAccount`, `fetchDraws`). Layouts are in the reviewed ABI
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
sui move test --path contracts                    # 157 tests, 33 fleet_budget
sui move test --path contracts/fleet_storage      # 25 tests against Walrus testnet-v1.59.0
make edge-test                                    # funding, exact-draw check, signer draw policy
python3 scripts/test_budget_ota.py                # in make test-ota: real daemon vs simulator
node scripts/test_fleet_budget.cjs                # in make test-manifest: estimate vs SF-054, on-ramp, builders, alerts
make test-budget-localnet                         # network none: upgrade, on-chain refusals, version gate, daemon draws
make test-budget-testnet SUIFLEET_WALLET=…        # public testnet, dedicated agent wallet, lock required
```

- **Move tests.** Limits, period resets, revocation, rotation, deregistration,
  pause, suspension, removal, ownership and cap checks, a compromised key
  draining at the maximum rate, and the version gate. For storage: exact
  storage splitting, non-deletable fleet ownership, quotas, write-fee caps,
  slippage, release/extend and version.
- **Isolated localnet** ([sf083-localnet.json](validation/sf083-localnet.json)).
  A base (main) package with an activated device is upgraded to head. The
  pre-upgrade device enrolls and draws. These refusals were recorded as
  failed transactions on chain:
  - the base package: `FunctionNotFound`;
  - above the float cap: 8;
  - revoked: `device_cap` 2;
  - paused: 3;
  - suspended: 7;
  - over the allowance: 9;
  - over the draw count: 10;
  - the previous version after a later upgrade's migration: 0.

  The device's attempt to use the owner cap is refused before execution. The
  real daemon draws three times over gRPC and then journals its limit.
- **Public testnet** ([sf083-testnet.json](validation/sf083-testnet.json),
  2026-10-21). The run used the dedicated agent wallet, chain `4c78adac`, and
  held `network:suifleet-testnet`. Every transaction's effects were checked.
  - **Publication.** The head `sui_edge` and `fleet_storage` packages were
    published with bytecode verified before signing. `fleet_storage` links to
    the live Walrus package. Run 1 published `sui_edge` and stopped before
    `fleet_storage`. Run 2 adopted that publication after on-chain checks,
    and nothing was republished.
  - **Budget.** A budget was created with a 0.06 SUI deposit. A throwaway
    device was enrolled and received exactly its 0.02 SUI float. It drew
    0.005 SUI: `GasDrawn`, net gas 1,988,855 MIST, now the estimate's draw
    cost.
  - **On-chain refusals:** above the float cap (8), after revocation
    (`device_cap` 2) and while paused (3).
  - **Real daemon.** It drew twice over the public fullnode, then journaled
    its period draw limit.
  - **SUI-only deposit.** SUI was swapped for WAL through the pinned
    exchange into fleet storage in one PTB. A minimum above the posted rate
    aborted (12). Storage was bought with the prepaid WAL.
  - **Fleet-paid upload.** The device registered and certified a 2 KiB blob:
    - exactly its encoded size, from fleet storage;
    - write fee 328,860 FROST from the prepaid WAL;
    - non-deletable and owned by the fleet object;
    - confirmed by 68 storage nodes and read back intact.

    Its second upload in the period aborted on its quota (6).
  - **Cleanup and cost.** The unused deposit was withdrawn and the device
    key swept. Net spend was 0.3197 SUI for the `sui_edge` publish (package
    storage) plus 0.1276 SUI for everything else, most of it the
    `fleet_storage` publish.

## Outstanding acceptance

Real card checkout and real wallet sessions are deferred to final external
acceptance in [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159)
and [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32). So
are Mainnet deposits, swaps and uploads, and physical devices. None of them
has run.
