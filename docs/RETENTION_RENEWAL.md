# Protected prepaid retention (SF-092)

SuiFleet hosts no retention scheduler. Browser actions and prepaid on-chain
policies are the product path. Worker examples below are optional customer-run
automation; funding alone does not promise execution. See [ownership](SERVERLESS_PRODUCT.md).

SF-083 is merged and integrated. This guide describes the finite prepaid policy,
independent executor and failure behavior implemented by PR #232. Verification
and its limits are recorded in [the validation receipt](validation/sf092-local.json);
required exact-head CI and independent review remain separate gates.

## Authority and finite reserves

The core budget remains SF-083 interface v1. Storage mutations use version 2.
`FleetStorage`, `BlobRecord`, existing public signatures and existing type origins
remain unchanged. An owner explicitly calls `migrate` on v1 storage; previous
version-1 mutation functions then reject it. New `RetentionKey`, `Retention`,
`RenewalKey`, `Renewal`, `PrepaidKey`, `PrepaidCountKey`, `ProtectedBlobsKey` and events have their own introduction-package origin.
Configure `NEXT_PUBLIC_FLEET_STORAGE_ORIGIN` to the original storage package,
`NEXT_PUBLIC_FLEET_STORAGE_PACKAGE` to the current call package and
`NEXT_PUBLIC_FLEET_RETENTION_ORIGIN` to the retention introduction package.
Never replace existing origins with an upgrade's call address.

The owner configures a separate executor address, safety margin, WAL allowance
per Walrus epoch, SUI reward allowance per epoch and reward per successful renewal.
`fund_retention` accepts WAL and SUI from any wallet without transferring authority.
Those balances are held in a dynamic field, outside discretionary storage WAL and
outside all device gas accounts. Legacy withdrawals/uploads/pool purchases cannot
consume them. `withdraw_retention` requires the owner cap and emits `RetentionRisk`;
the dashboard requires acknowledgment that the displayed blob expiries become at
risk. The owner can still withdraw, release blobs or change policy. Withdrawing WAL
cannot undo already purchased capacity; removing the SUI reward reserve can prevent
the executor from applying that capacity before the blob expires. Reauthorizing or
releasing a prepaid target deliberately destroys its unused paid resource and emits
a risk event; no WAL refund is promised.

`authorize_renewal` gives one existing fleet blob a finite target and WAL quote
ceiling. The owner must cover **all** fleet blobs, a conservative superset of the
active artifact/recipe/configuration/dependency, rollback/recovery, site/manifest
and fleet-history closure. External blobs are not magically protected: export,
verify and import/re-upload those references under fleet custody. The dashboard
lists every fleet record and unprotected entries. Owner-selected targets are not
an automatic dependency resolver for references outside this storage.

Discretionary purchases require complete fleet coverage, actual purchased renewal
capacity for every pending target and balances covering execution rewards. A bounded
on-chain index checks that no protected blob has expired; expired records require
owner restoration/replacement or explicit release before new admission. Legacy registrations stop
while retention is configured. Owner uploads use `register_retained_fleet_blob`
to register and reserve a new target atomically; insufficient reserves abort the
whole transaction. SF-082 adds owner `configure_device_retention` admission for
native daemon archives: `register_blob` atomically prepays their owner-selected
finite target and maximum WAL quote. Without that explicit approval device
registrations pause. `clear_device_retention` pauses new archives without deleting
history. Device receipts remain Sui transactions governed by their gas accounts.

WAL FROST and SUI MIST are separate tokens. No conversion is assumed. The owner
buys WAL through the supported exchange/payment route, then deposits it.
`prepay_renewal` buys the actual Walrus `Storage` resource for `[old_end, target)`
using the owner's maximum WAL ceiling. Walrus measures and charges its current
protocol price in that transaction. A higher price, insufficient coins or invalid
horizon aborts atomically; a stale estimate cannot admit discretionary writes.
The dashboard authorizes and prepays in one transaction; retained registration
also prepays atomically. The resource remains in fleet custody under `PrepaidKey`;
`Renewal.max_fee = 0` identifies a paid pending target. The actual fee is emitted
by `RenewalPrepaid`, charged against the per-epoch WAL allowance, and removed from
pending cash ceilings. Executor gas stays separately reserved and bounded.

The worker's fresh price projection is informational for already paid capacity:
price changes do not charge it again. A pending target without purchased capacity
produces `prepayment_required` and never spends executor gas. Execution consumes
only the authorized resource and pays the bounded success reward. A purchased
resource does not schedule its application or rescue an already expired blob.
Finite targets, protocol horizons, executor liveness, gas prices, owner overrides
and future policy renewal remain explicit limits; there is no indefinite guarantee.

## Run the independent executor

The owner runs Python 3, Node and the repository's frozen dApp dependencies on a
trusted host. No open browser, server sponsor, owner wallet or owner capability is
needed by the worker. Use a dedicated executor key, with only a bounded gas float;
never point it at an owner keystore. Its permissions on chain permit only selected
blob renewals. The worker pays gas itself; only a successful extension earns the
configured bounded SUI reimbursement. Failed/no-op/duplicate calls earn nothing.

1. Install dependencies with `make dapp-install`.
2. Configure/migrate storage in the Network budget page. Select the dedicated
   executor address, finite blob targets and measured quote ceilings. Deposit
   enough isolated WAL and SUI reserves before discretionary writes.
3. Put the dedicated executor's `suiprivkey...` (or 32-byte hex seed) in a 0600
   file outside the repository. Keep its parent directory private.
4. Write private JSON configuration, replacing every placeholder:

```json
{
  "network": "testnet",
  "chain": "4c78adac",
  "rpc": "https://fullnode.testnet.sui.io:443",
  "package": "0x<CURRENT_STORAGE_CALL_PACKAGE>",
  "storage_origin": "0x<ORIGINAL_STORAGE_PACKAGE>",
  "retention_origin": "0x<RETENTION_INTRODUCTION_PACKAGE>",
  "storage": "0x<FLEET_STORAGE>",
  "executor": "0x<DEDICATED_EXECUTOR_ADDRESS>",
  "executor_key_file": "/private/executor.key",
  "system": "0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af",
  "walrus_package": "0x7d3b2086808a62bbc44d18c6b1d407973d77dd26946735873092c2f5650b4611",
  "gas_budget": "10000000",
  "spend_ceiling": "100000000",
  "low_wal": "1000000"
}
```

5. Create a private state directory and run:

```bash
install -d -m 700 /private/retention-state
python3 scripts/retention_worker.py --config /private/retention.json \
  --state-dir /private/retention-state
```

The process polls hourly; run it under the owner's service manager for
liveness. `--once` executes one reconciliation for diagnostics. The private
`retention.json` state file records snapshots, independent failures, successful
digests/epochs and notification status. A file lock permits one worker. Atomic
rename and fsync make the signed transaction durable **before** submission.
Restart reuses its original bytes/signature/digest and reconciles successful
effects plus the authoritative new expiry. Submission alone is never success.

The worker allows three attempts per authorized target with exponential backoff,
at most 1024 blob records and 4 MiB of state. It reserves each new transaction's
whole gas budget against its non-replenishing spend ceiling. Refunds and top-ups
do not reset that ceiling. Inspection and an explicit owner-managed new allowance
are needed after exhaustion; preserve uncertain signed transactions and the old
journal. Never delete state to clear an uncertain operation. Policy/configuration
changes fail closed against the stored configuration binding; migrate the private
state deliberately after reconciling pending effects.

Renew only while `current_epoch < exclusive_end`, within the selected safety
margin and Walrus's actual extension horizon. Targets/revisions/old end are checked
on chain; shared storage serializes concurrent attempts. The dashboard shows exact
expiry epochs, last successful renewal epoch and projected dates/runway. Dates
assume the identified epoch schedule stays unchanged. An expired blob requires
restoration, not a longer metadata reference.

## Optional alerts and honest delivery

To send alerts without a browser, configure an owner-run HTTPS endpoint and add:

```json
{"notifier":{"url":"https://alerts.example/ingress","secret_file":"/private/notifier-hmac.key"}}
```

Merge that `notifier` member into the configuration **before starting a new state**.
Use at least 32 random secret bytes in a 0600 file. No keys belong in git, issues or
public artifacts. The receiver must authenticate `X-SuiFleet-Signature` as HMAC
SHA-256 over the exact JSON body, validate timestamp/schema and persist the message
ID to reject replay. It must return 2xx only after durable acknowledgment. Payloads
contain only schema, an opaque ID scoped to the immutable worker configuration,
code, epoch and timestamp; no blob IDs,
private content or full fleet metadata. HTTPS validates server identity; redirects
are refused. An acknowledgment proves endpoint acceptance, not a human reading it.

The worker persists queued/attempted/acknowledged/failed status, retries at most
three times and retains at most 256 alerts. Full alert storage applies backpressure;
export/acknowledge it deliberately, never silently discard it. No notifier, a stopped
worker, lost gas, provider outage or unavailable network means **no guaranteed
warning or renewal**. Monitor the private state/service independently. Set optional `budget_origin` to the verified core FleetBudget introduction package
to include operation-balance, paused-budget, empty device reserve and exhausted
current-period allowance alerts. The worker verifies current on-chain account
index, types and budget/device bindings. Daemon funding journals and telemetry
also expose why a draw is blocked. No purchase-provider webhook is trusted as
on-chain funding.

## Failure, queues and restoration

A failed/uncertain card checkout does not credit the fleet. Confirmed on-chain
deposit effects remain authoritative, independent of provider UI state. Existing
prepaid funds, identities, storage and installed applications survive purchase
failure. Alternate-wallet reserve deposits grant no fleet administration.

The daemon retains unreported receipts/deferrals across restart, up to 1024 pending
items or 8 MiB (with space for a bounded new outcome). It rejects new admission
before changing application state or advancing the event cursor when full. Errors
are bounded to 4096 UTF-8 bytes with an explicit truncation label. Existing pending
receipts are never age-evicted; confirmed reports free backlog slots. This bounds
the pending queue, not the entire historical journal, artifact cache or filesystem.
Existing stream/log subsystems retain their own configured disk/backpressure rules.
Funding exhaustion pauses new deployment admission; reports retry before new
admission and resume after confirmed funding/allowance recovery. No payment failure
uninstalls apps, revokes identities, reboots a device or triggers platform rollback.

Keep customer copies of artifacts, exact recipe/configuration/dependency bytes,
site/manifest/history exports, digests and decryption recovery material securely.
When a blob expired, verify the retained bytes' authenticated digest, re-upload and
certify them through the authorized owner upload flow, verify actual storage effects
and new expiry, and publish an authorized new reference/deployment if its ID changed.
The daemon still verifies recipe/artifact digests and device/target/decryption scope;
never replay ambiguous destructive lifecycle work merely because funding returned.
The real-daemon budget simulator covers expired bytes, a digest-verified retained
customer copy, a newly authorized reference and restart without duplicate apply.
That simulator does not establish real Walrus re-upload/certification or a physical
recovery result; those distinctions remain explicit in acceptance evidence.

Sui retains authority/references/accounting, not recoverable binary content. Walrus
retention is finite; Seal/provider availability and credentials are separate.
Revocation cannot remotely erase data already decrypted by a recipient. Platform
tools remain responsible for OS installation, reboot and firmware recovery.
Physical/manual-wallet/production-provider/mainnet acceptance stays unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

## Local verification and open gates

`make test-contracts`, `make test-retention`, `make edge-test`, `make test-ota`,
`make check-interfaces`, `make dapp-typecheck` and `make dapp-build` are offline
software gates. `make test-retention-upgrade` must run inside a disposable
network-none dev container with the warm pinned Move cache and Git history,
including SF-083 merge `3e796f7`. For a linked worktree, mount its Git common
directory read-only at the same absolute path as well as the checkout at
`/workspace`. The fixture publishes the real pinned WAL/Walrus dependencies,
creates v1 storage, performs a compatible v2 upgrade/migration and checks an old
package withdrawal's actual failed effects. The fixture creates a deterministic Walrus committee only inside the isolated
copied dependency. It populates exact v1 storage through actual WAL deposit,
pool purchase and blob registration, then verifies unchanged pool/balance/counter
bytes and wrapped fields/type origins after upgrade. It does not substitute that
fixture's initialization for public-network provider acceptance.
Fixture manifests resolve the same cached pinned framework sources locally so the
disposable chain never accesses the public network.

`python3 scripts/live_fleet_budget.py --retention --wallet PATH --build-sui PATH
--network-sui PATH --evidence PRIVATE_PATH` adds real restricted-worker renewal
to the existing bounded SF-083 testnet runner. Read [TESTNET.md](TESTNET.md), acquire
and renew the global mutation lock, verify chain/account/authority and release
promptly. It reserves each transaction against the non-replenishing allowance,
checks effects and actual blob expiry, and records net gas. Actual testnet effects, certification/readback, restricted renewal and original-digest
restart evidence are recorded in the validation receipt. A failed preparatory run
is retained as failed evidence, including its cost; no returned digest alone was
counted as success. The corrected host wallet configuration is
`~/.config/suifleet/testnet/agent/client-host.yaml`, the same authorized account and
original host keystore (no key copy or rotation).

For a stopped acceptance runner with an original durable renewal, the bounded
`scripts/live_retention_resume.py` probe takes `--original-evidence`, `--state-dir`,
`--wallet`, `--build-sui`, `--network-sui` and `--evidence`. It requires the shared
mutation lock, verifies the original owner/fixture, charges all existing executor
gas against its allowance, and only resubmits the original signed digest. It never
clears intent, republishes or faucets. Reopening the worker after confirmation
must produce no extra transaction. This is an acceptance recovery tool, not a
production service or an owner-policy bypass.

Normal exact-head CI/testnet and independent review remain mandatory. Physical,
manual real-wallet, mainnet and production-provider acceptance remains explicitly
unrun in #32/#159.

### Recovering executor and alert outages

New signed transactions are limited to three preparations per authorized target
and the configured non-replenishing gas ceiling. An uncertain transaction keeps
its original signed bytes: receipt reconciliation and idempotent resubmission
continue with exponential backoff capped at one hour, until exclusive blob expiry.
If a gas-payment or owned-input reference has advanced and a second digest lookup
still finds no effects, those signed bytes cannot execute again. The worker clears
that intent and may prepare a replacement within the same three-preparation and
non-replenishing gas bounds. An unchanged or unreadable reference never proves
failure. Each fresh snapshot still gates renewal on the actual expiry; a missing
receipt never establishes success.
`renewal_retry_exhausted` warns after three submissions; it does not abandon prepaid
capacity. A stopped worker still provides no renewal guarantee.

Stop the worker before acknowledging old alerts. Inspect its private
`retention.json`, then export and explicitly acknowledge entries older than a chosen
epoch (including undelivered or retry-exhausted entries):

```bash
python3 scripts/retention_worker.py --config /private/retention-config.json \
  --state-dir /private/retention-state --archive-alerts-before 560 \
  --alert-export /private/alerts-before-560.json
```

The export must be a new file; it is private and flushed before entries are removed
from durable state. Keep the export for audit. Alerts at or after the cutoff remain
queued, and the worker can admit new warnings after restart. Archiving an active
condition can allow it to be queued again; prefer past epochs. A full queue retains
all unacknowledged warnings and records backpressure locally; delivery of new alerts
is unavailable until the owner frees space. Monitor this state as well as the relay.

Owner `extend_blob` must not be used while prepaid renewal capacity is attached:
changing its expected expiry strands that capacity and delegated execution aborts.
Use the prepaid executor first. Reauthorization discards capacity without refund;
inspect pending intent before any owner policy/reference change. During funding
outages the daemon retries the unprocessed one-shot event on a later poll; that
cycle can defer bookkeeping reconciliation, while installed applications persist.

## Archive capacity and expiry maintenance

Protected storage admits at most **1,024 protected blobs fleet-wide**, shared by
archives, releases, recipes and other owner data. Only owner `release_blob` frees
an entry; renewals and expiry do not. Ten devices at daily cadence consume 300
slots per 30-day month, filling an otherwise empty index in 102.4 days; diagnostic,
crash and owner uploads reduce that horizon. At capacity new registration aborts
with `EOverQuota`. Every registration reads all protected blobs; gas grows linearly
with history. Protocol gas/object limits may stop admission before 1,024. The small
fixture's 27,075,739 MIST archive cost is an observation, never a fleet-scale ceiling.
No measured worst-case gas is available. The budget estimator projects slot usage,
not that unknown gas growth; simulate against current state and review actual costs.

The device admission target is an absolute epoch shared by new archives. It is
not a rolling retention window. Any expired protected blob halts registration
fleet-wide with `EInvalidEpochs`; funding alone cannot repair expiry. The daemon
retains queued data within its disk bounds, not an infinite offline backlog.

1. Monitor the live protected count, each actual exclusive expiry, prepaid targets,
   executor journal and disk backlog. Allow slots for all fleet uses and emergency
   traffic. Do not treat an unavailable query as zero usage.
2. Before expiry, ensure the separate executor applies paid capacity successfully;
   verify actual effects and new blob expiry. Then explicitly authorize/prepay a
   later target for existing blobs if needed, within Walrus's horizon. Changing
   `configure_device_retention` affects new admissions only. Do not use owner
   `extend_blob` while prepaid capacity remains attached.
3. Before capacity is exhausted, export and authenticate history and review active
   application/recovery references. Only the owner may retire obsolete blobs:
   construct `fleet_storage::release_blob(storage, budget, cap, blob_id)` and transfer
   its returned Blob to the verified owner (or burn an expired Blob using the pinned
   Walrus API). Verify effects, reduced protected count and remaining references.
   Release discards attached paid capacity without WAL refund and makes historical
   archive reads unavailable; preserve required ciphertext and decryption material
   privately. Anchor metadata is not deleted. Never release a required rollback,
   recipe or recovery reference just to free a slot.
4. If expiry already halted admission, explicitly release **every expired protected
   record**, after the same backup/reference review. Expired bytes need restoration,
   authorized re-upload/certification and new references; a target change alone
   cannot restore them. Reconcile remaining coverage/reserves and verify an actual
   new admission before resuming uploads. Clearing device admission does not clear
   expired protected records or enlarge the index.

This is a finite owner-maintained policy. Continuous unattended growth requires a
separately reviewed scaling design; no automatic deletion or rolling window is
implemented. The write cadence and owner quotas bound upload frequency/spend, not
the lifetime of a growing protected fleet.
