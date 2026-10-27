# Bulk group membership (SF-171)

Operators add or remove many enrolled devices to or from one or more groups
from the **Devices** page, instead of one wallet approval per device in the
group's Manage dialog.

## Using it

1. Select devices on the Devices tab: tick rows, filter by name, serial,
   architecture or group and use **Select N shown**, or **Paste a list** of
   serials, names or object IDs. Pasted entries match exactly
   (case-insensitive: object ID, then serial, then name). Entries that match
   nothing, or match several devices, are listed and never guessed.
2. Choose **Change groups…**, then **Add to groups** or **Remove from groups**
   and one or more groups.
3. Review the plan before approving. It lists every change per group, the
   devices skipped because they are already members (add) or not members
   (remove), and the number of transactions, which is the number of wallet
   approvals.
4. Approve. Transactions are sent one at a time. The run stops at the first
   failure.

### Architecture

When adding, a device whose architecture no current member of the group has
is listed as a mismatch and excluded. Tick **Also add devices whose
architecture differs** to include it explicitly. A group with no members has no
architecture to mismatch.

With the batch entry points (below), each add call carries the architecture
the operator reviewed. The chain checks every device's registered
architecture against it, so a wrong or stale read cannot slip in a device of
another architecture.

### Partial failures

Each transaction is atomic. After a run the dialog reports, per device and
group:

- **changed**: in a transaction whose effects were confirmed, or in the
  failed transaction but shown as changed when membership was re-read;
- **not changed**: in the failed transaction and not changed on re-read;
- **unknown**: in the failed transaction, but membership could not be re-read;
- **not attempted**: in later transactions, which were never sent.

Nothing is retried automatically. Reviewing again plans only the remaining
changes, because devices already in the requested state are skipped.

## Limits

| Path | Changes per transaction |
|---|---|
| Managed fleet, batch entry points | 100 (`customer::MAX_GROUP_BATCH`, enforced on chain) |
| Managed fleet, older package | 100 single-device calls |
| Wallet-administered groups | 25; a device appears at most once per transaction (it may create its inbox) |

Each change touches a few dynamic fields, so these bounds keep a transaction
well under the per-transaction dynamic-field and command limits.

## Contract entry points

Additive `customer` functions; existing signatures and layouts are unchanged:

- `add_many_to_group(customer, fleet, group, registry, device_ids, arch: Option<String>)`
- `remove_many_from_group(customer, fleet, group, device_ids)`

Both require the same fleet administrator and fleet inventory as
`add_to_group`/`remove_from_group`, which they call for each device. They take
1 to 100 IDs (abort `EBatch` = 7 otherwise) and are all-or-none: a duplicate,
a non-member removal or a foreign-fleet device aborts the whole call. When
`arch` is set, every device must be in registry custody with exactly that
architecture (abort `EArch` = 8). The registry must be the customer's. Each call
emits one `customer::GroupBatchApplied { fleet_id, group_id, added, count }`
event besides the usual per-device membership events. `device_registry::arch`
is a new read helper.

The optional `group_batch` type-origin group (`customer::GroupBatchApplied`)
tells clients the deployment has these entry points. Manifests without it keep
working: the dApp then sends one `add_to_group`/`remove_from_group` call per
change, in the same bounded transactions, without the on-chain architecture
check. Older packages remain callable and keep their single-device behavior.

## Tests

- `make test-contracts`: batch add/remove accounting, maximum size, above the
  bound, empty batches, architecture mismatch, unregistered IDs, duplicates,
  non-members, foreign fleet devices, non-administrators, a foreign registry and
  indexed managed groups.
- `node scripts/test_group_batch.cjs` (in `make test-manifest`): filtering,
  pasted lists, review plans, architecture mismatches, per-transaction bounds,
  transaction shapes and partial-failure outcomes.
- `make test-group-batch-localnet` (disposable network-none container with Git
  history and a warm Move cache): a real base→head upgrade, then the dApp
  builder against pre-upgrade customer objects, on-chain refusals (architecture,
  unregistered ID, bound, duplicate), a base-package call after the upgrade, bulk
  removal and deregistration of a formerly grouped device.
- `scripts/live_group_batch.py` (dedicated agent wallet, `network:suifleet-testnet`
  lock): an ephemeral publication of the contracts, four fleet devices, an
  on-chain architecture refusal, one batch adding all four (3 `x86_64` +
  1 `aarch64`, each pinned to its architecture), one batch removal and cleanup.
  Public-testnet run at `435f73ba`: `docs/validation/sf171-testnet.json`
  (7 transactions, 0.0449 SUI net, reusing publication
  `5pSxFqT1…pvGG`). The first attempt
  (`docs/validation/sf171-testnet-attempt1.json`) published the package
  (0.603 SUI) and stopped when device registration exceeded its 0.08 SUI gas
  budget; registration of four devices costs about 0.107 SUI gross.
