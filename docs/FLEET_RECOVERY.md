# Customer fleet recovery and dependency outages (SF-091)

This is the paid-pilot recovery and incident runbook for the serverless product:
the static dApp, deployment manifest, customer custody, Walrus content, Seal
policy and device journals. It is executable. Every procedure below names its
tool, and the drills in [Validation](#validation-and-limits) rehearse it on
isolated infrastructure.

Two rules apply everywhere. **Recovery never weakens confidentiality.**
**Recovery never assumes that deleted plaintext, expired content or a lost
private key can be recovered.** Sui holds authority, references and accounting,
not recoverable binaries. Walrus retention is finite. Seal releases keys only
under the current on-chain policy.

This runbook targets SF-082 (#160) as merged to main (`39ea150`). The drills and
the read-only Testnet check were rerun on that merged main.

## 1. Recoverable state and its source of truth

An operator machine holds no fleet state that cannot be rebuilt. Authority lives
in customer wallets, and records live on Sui and Walrus. Loss of the operator
machine therefore loses only the private runtime state listed as such below.

| State | Source of truth | Backup owner | Restore requirement |
|---|---|---|---|
| Deployment manifest: chain, call package, per-type origins, shared IDs | The release's public `deployment.json`, rechecked against the chain by `verifyDeployment` | SuiFleet release owner (public in git) | Its canonical digest must equal the Site build receipt (`fleet_recovery.py restore`). Never re-derive origins from the call package. |
| Fleet-data, Seal and storage bindings | Public JSON files (`sf082-testnet-{fleet,seal,storage}.json` for the Testnet fixture) | SuiFleet release owner | Canonical digests must match the build receipt. A weaker threshold or a foreign object is refused. |
| Site revision and rollback targets | Site build receipt, Site publication receipt (phases, blob IDs, resource maps), on-chain Site object | SuiFleet release owner. The previous export and its receipt are retained privately. | The latest verified phase must serve the build receipt's exact resource map. Rollback only to a phase that was read back on the same Site. |
| Site publisher identity and journal | Private runtime directory of `live_walrus_site.py` | SuiFleet release owner, kept offline and encrypted | Never in git or evidence. Losing it means a new publisher identity, and the existing Site then needs its owner to authorize updates. |
| Customer authority: DeviceOwnerCaps, groups, deployment creator, BudgetOwnerCap, storage/retention owner | The customer wallet or multisig, on chain | **Customer** | Restore the wallet from the customer's own recovery material. SuiFleet never holds it. Losing every signing authority has no recovery path ([handover](FLEET_HANDOVER.md)). |
| Seal provider configuration | Public key-server object IDs and threshold, plus provider credentials where used | Customer for credentials. Public IDs are in the release. | Credentials are restored from the customer's secret store and are never exported. Key servers re-read the policy from the chain. |
| Encrypted artifact/recipe references | Deployment records and recipe digests on chain. Ciphertext on Walrus. | Customer keeps authenticated plaintext copies of every artifact, recipe and configuration | The chain reference alone cannot rebuild bytes. Re-upload must match the recorded digest ([retention](RETENTION_RENEWAL.md#failure-queues-and-restoration)). |
| Fleet archives (device logs/diagnostics) | `FleetData`/`DeviceData` anchors on Sui, with ciphertext on Walrus until its live record's end epoch | Nobody else. The device spool is deleted once a verified anchor is read back. | Readable only while the blob is live and the key epoch is current. Export authorized plaintext **before** expiry, rotation or revocation. |
| Retention policy, targets, reserves and prepaid capacity | `FleetStorage` dynamic fields | Customer (owner cap) | Read with `fetchRetention`. Worker state (`retention.json`, alert queue) is private. Back it up only together with its configuration binding. |
| Budget balances, device reserves and device floats | `FleetBudget`, `GasAccount`, and coins at each device address | Customer | Floats live on the device key. Only that key can sweep them. |
| Device journal, spool, component state and signing key | `/var/lib/suifleet` and the signing service on each device | Customer/platform integrator | Preserve across platform restarts. Never copy a device key off the device. A lost key needs `rotate_device_key` to a new key. The old key stays lost. |
| Optional alert relay state | `fleet_alert_relay.cjs` private state file | Customer | Its state is not a trust input. If lost, it rebuilds from chain reads, with possible duplicate alerts. |

Backups that contain keys, sessions, provider credentials, wallet
configuration or runtime journals go to encrypted customer-controlled storage
only. They never go to git, issues, CI artifacts or public evidence.
`fleet_recovery.py` refuses inputs that look private.

## 2. Restore from a clean operator environment

Use a new machine or container with only the repository at the reviewed
release and its locked dependencies (`make dapp-install`). Copy no wallet,
cache or runtime directory from the old machine.

```bash
python3 scripts/fleet_recovery.py restore \
  --manifest docs/validation/sf082-testnet-deployment.json \
  --build-receipt docs/validation/sf082-site-build-a.json \
  --publication-receipt docs/validation/sf082-site-publication.json \
  --fleet-data docs/validation/sf082-testnet-fleet.json \
  --seal-settings docs/validation/sf082-testnet-seal.json \
  --fleet-storage docs/validation/sf082-testnet-storage.json \
  [--export /retained/static-export] --env-out /new/restored.env
```

The tool verifies every binding against the retained receipts and names the
current verified release and earlier releases. With `--export`, it checks a
retained static export byte for byte before anyone serves it again. The env
file holds only public `NEXT_PUBLIC_*` values. Source it with `set -a; . FILE`
for `pnpm build`, or pass the same public files to `build_walrus_site.py`.

Then resume management:

1. Open the restored dApp or Site and connect the **customer** wallet. Owned
   caps prove authority. Restoring the dApp grants none.
2. Discover the fleet from the chain index (`discoverFleet` pages
   `listFleetDeviceIds` and fails closed when pages are truncated or duplicated).
   Verify health (`fetchChainHealth`) and archive availability
   (`assessArchive`). `make live-fleet-recovery` does this read-only against
   Testnet. With `SUIFLEET_WALLET`, it also finds the fleet's BudgetOwnerCap in
   that wallet and decrypts under the owner policy with a signed five-minute
   session. It sends no transaction.
3. Before creating anything, compare the customer's retained deployment records
   with the chain (`resumeDeployments`). Records that already exist are resumed,
   whether matched by ID or by identical content and targets. Only records from
   verified creators count. Missing records are recreated through the normal
   reviewed flow. Unknown on-chain records are investigated, not deleted.
4. Restart owner-run services from their private state: the retention worker
   with its original configuration binding, and the optional alert relay. Never
   delete a worker state or a pending signed transaction to clear an uncertain
   outcome.

## 3. Outage and incident playbooks

`assessArchive` attributes every refusal. A dependency outage is **resumable**.
Chain-state refusals and tampering are **not**. Each state maps to one recovery
action:

| Observed | Classification | Action |
|---|---|---|
| Fullnode RPC errors (`dependency: rpc`) | Resumable outage. Health shows `unavailable`, never `offline` or resolved. | Retry, or switch to another fullnode for the **same** chain (the chain ID is rechecked). Devices queue receipts and defer new work. Installed components keep running. |
| Walrus committee read fails (`walrus`) | Resumable | Retry. No expiry is inferred. |
| Aggregator 5xx, network error, connection reset or timeout while the body streams, or 404 on a live record (`aggregator`) | Resumable | Use another aggregator. The chain record, not the aggregator, decides expiry. Devices retry the download later without failing the deployment. |
| Key server unreachable (`keyserver`) or expired session (`session`) | Resumable | Retry, or sign a new session. A provider set or threshold that differs from the pinned Seal settings, or a Seal request/ciphertext refusal, is `invalid` (an incident), not an outage. Installed components keep running offline. A **new or reinstalled** confidential version needs reachable key servers ([offline policy](CONFIDENTIAL_DELIVERY.md#lifetimes-caching-offline-use-and-revocation)). |
| Key server `NoAccess` (`access_denied`) | Authority. Not an outage. | The owner grants support access explicitly, or the owner reads. |
| Live record ended (`expired`) or released (`released`) | **Not recoverable** for device archives | Device archives are permanently lost. Artifacts and recipes are restored from the customer's authenticated copies: re-upload, verify certification and expiry, then publish an authorized new reference. |
| Unmigrated v2 registration aborts with `EInvalidEpochs` (expired protected blob) or `EOverQuota` (1,024 protected blobs) | Owner migration to v3 removes these fleet-wide blockers | Follow the [migration runbook](RETENTION_RENEWAL.md#archive-capacity-and-expiry-maintenance). On v3, expiry is handled per blob; do not release records merely to restore unrelated admission. Release removes archive readability and discards attached capacity without a WAL refund. Verify coverage, reserves and the new archive's finite future target before retrying. |
| Anchor key epoch older than the fleet's (`key_epoch_rotated`) | **Not recoverable** through policy | Rotation refuses new key requests for older epochs, including the owner's. Export first. |
| Device revoked or missing (`device_revoked`) | **Not recoverable** through the verified reader | Export a device's history **before** revoking it. See the findings below. |
| Digest, signature or binding mismatch or an oversize body (`invalid`), or missing index (`missing`) | Incident | Never trust the bytes. Preserve evidence and escalate. Tampered bytes are refused before any key request. |

**Bad frontend release.** If a served export differs from its build receipt,
the release is bad. If a reviewed release misbehaves, run
`fleet_recovery.py rollback --build-receipt CURRENT --retained-build-receipt
PREVIOUS --publication-receipt RECEIPT`. CURRENT must be the receipt the
publication names and its latest verified phase serves. It accepts only an
earlier phase that was verified on the same Site and binds the same chain, call
package, deployment, fleet, Seal and storage settings. A different, added or
removed binding is a deployment change, not a rollback. Then run
`live_walrus_site.py --operation rollback` under its usual authorization and
preflight. Verify every resource and route before any domain change. Rollback
costs new transactions and storage.

**Compromised or revoked device.** Pause admission and rollouts and suspend the
device's gas account (`set_device_suspended`). Then `revoke_device`. The daemon
stops applying and reporting, and the verified dApp shows it unreported.
Revocation cannot recall plaintext or keys already released, cannot erase
decrypted data and cannot uninstall delivered software. Handle the device
physically, or through the platform. Rotate the key epoch if future archives
must exclude keys the device could have obtained. Rotate to a new key
(`rotate_device_key`, then `issue_cap`) or deregister. A lost device key is the
same case: the old key and any float coins at its address are unrecoverable.
The daemon refuses a replaced key: its state is bound to the original identity,
and even on a fresh state directory the DeviceCap is not owned by the new key.
Use [handover (SF-089)](FLEET_HANDOVER.md) to move administration while a signer
remains. Losing every signer is unrecoverable.

## 4. Pilot recovery objectives, monitoring and escalation

These are **pilot targets**, measured on isolated infrastructure and in
read-only Testnet checks. They are not a production SLA. Public fullnodes,
aggregators, Walrus and Seal providers offer no SuiFleet commitment, so no
guarantee is implied from them.

| Event | Target recovery time | Target data loss | Measured in drills |
|---|---|---|---|
| Operator machine lost | Verified config restored and fleet discovered within 1 hour of a clean machine being available | None: no fleet state lives only on the machine | Restore < 1 s; Testnet discovery 0.3–1.0 s; ciphertext check of every archive 2.8 s; complete owner-policy check 6.4–9.4 s |
| Fullnode, aggregator, key-server or Walrus outage | Normal operation within 3 poll intervals after the dependency returns | None for anchored, queued or installed state | Device resume 2.0–3.2 s at a 1 s poll; browser verified read on first retry |
| Funding failure (declined purchase, exhausted allowance, paused budget, empty reserve) | Resume within 3 poll intervals after confirmed on-chain funding | None while the pending queue is within 1024 items / 8 MiB | 2.7 s after refill; no duplicate apply |
| Platform daemon restart | Installed version running again within 1 poll interval plus component startup | None | 1.2 s |
| Bad frontend release | Rollback within 4 business hours of authorization | None (retained export) | SF-082 Testnet rollback phase recorded |
| Blob expiry without renewal | Not recoverable for device archives | Everything not exported or copied | Observed: expired archives classified `not_recoverable` |

Monitoring: the dApp's chain alerts, budget and storage alerts and retention
facts, run whenever it is open. Off-browser monitoring needs the optional
`fleet_alert_relay.cjs` and the retention worker's notifier. Watch those
processes and their private state too, because a stopped worker or relay means
no warning at all. Devices expose blocked funding in their journal and in
telemetry.

| Role | Owns |
|---|---|
| Customer fleet owner | Wallet/multisig custody, funding, retention targets, artifact copies, provider credentials, relay/worker hosting, revocation decisions |
| SuiFleet pilot support | This runbook, release receipts, Site rollback, triage within pilot business hours, incident evidence |
| Infrastructure providers (fullnode, Walrus, Seal) | Their own availability. Escalate through their public channels, with no SuiFleet guarantee. |
| Platform integrator | OS install, reboot, firmware recovery and physical device handling |

Declare recovery only with this evidence: the restore summary binding the
receipts; chain-ID-checked discovery of every expected device; verified health
or an explained state for each device; `assessArchive` results for the
archives in scope; resumed receipts with execution effects, not just digests;
no unexplained duplicate deployment; and, for rollback, the full Site
resource/route readback. List anything permanently lost.

## 5. Customer exit and export

Use only existing APIs:

- **Public records.** `exportPublicRecords` (library) or `make live-fleet-recovery`
  writes fleet bindings, support grants, cadence, per-device health status and
  every archive reference: blob IDs, ciphertext SHA-256, signer, epochs. It
  writes no ciphertext, plaintext or keys. Deployment events, receipts and
  handover events are public chain reads (`fetchDeploymentEvents`,
  `readReceipts`).
- **Authorized decrypted data.** The `/data/` page opens archives one at a time
  with a signed owner or support session. Export everything needed **before**
  expiry, key-epoch rotation or device revocation.
- **Balances.** The owner withdraws unallocated budget funds (`withdraw`),
  reclaims device reserves (`reclaim_reserve`) or closes accounts
  (`remove_device`, which returns the reserve to the budget). The owner also
  withdraws storage WAL (`withdraw_wal`) and retention reserves
  (`withdraw_retention`, which emits a risk event). Device float coins can be
  swept only with each device key.
- **Storage obligations.** Walrus storage is a consumed purchase, not a deposit.
  Prepaid renewal capacity is not refunded. `release_blob` plus `burn` after
  expiry returns only the Sui object-storage rebate. Fleet-data anchors and
  public deployment metadata stay on chain: there is no pruning API.

## 6. Payment failure (SF-092 scenarios)

The integrated drill covers each case:

| Scenario | Observed |
|---|---|
| Failed funding purchase | No credit and no alert change. The owner's top-up resumes the worker. |
| Exhausted allowance, paused budget or empty reserve on the device | New admission pauses and receipts queue. The installed supervised component keeps the same PID and is not uninstalled or restarted. The daemon process is not restarted. Funding recovery resumes with no duplicate apply. |
| Executor or notifier unavailable | The original signed intent is reused after restart. Retries and notifications are bounded. The deadline stays visible. |
| Protected reserve depleted | Reported as underfunded. Discretionary writes pause. |
| Top-up before expiry | The owner extension keeps the archive readable beyond its original anchor epoch. |
| Expiry without renewal | Device archives become `expired`/`not_recoverable`. On migrated v3 storage, expiry does not halt unrelated registrations; unmigrated v2 retains the fleet-wide expiry scan until owner migration. Expired artifacts are never applied. A digest-verified customer copy plus an authorized new reference restores the deployment once, across a restart. |

**Observed loss.** Expired device archives (logs and diagnostics) cannot be
reconstructed. An artifact, recipe or configuration without a customer copy
cannot be reconstructed. Anything with an authenticated copy is restorable.
SuiFleet never uninstalls applications, reboots a device or triggers platform
rollback because of a payment failure. Missing provider access blocks only
fresh decryption and new confidential installs.

## 7. Implementation findings

These are reported on #178 for Codex to triage. No tickets were created.

1. There is no bulk export of authorized decrypted archives. `/data/` opens one
   archive at a time.
2. The verified reader refuses every archive of a currently revoked device.
   There is no owner-only historical read mode, so history must be exported
   before revocation.
3. There is no consolidated statement of balances and obligations: budget,
   reserves, device floats, storage WAL, retention reserves and prepaid
   capacity. Each must be read separately.
4. Fleet-data anchors are permanent, with no pruning API. Exit leaves that Sui
   metadata, and its storage cost, on chain.

## Validation and limits

- `make test-fleet-recovery-drill` runs all drills and writes one receipt with
  durations ([recorded receipt](validation/sf091-recovery-drill.json)). The
  parts are: `scripts/tests/test_fleet_recovery.py` (restore and rollback
  against the real SF-082 Testnet receipts); `scripts/test_fleet_recovery.cjs`
  (real signatures and Seal SDK ciphertext, with fixture chain, aggregator and
  key servers and injected faults); `scripts/test_recovery_ota.py` (real
  daemon); and the existing SF-092 retention-worker and budget simulators.
- `make live-fleet-recovery SUIFLEET_WALLET=…` is a read-only public-Testnet
  confirmation ([receipt](validation/sf091-testnet-recovery.json)). No
  transaction or gas is used.
- Not covered: production Seal providers, real IdP or extension-wallet
  sessions, a physical Pi, power loss and Mainnet. These stay unrun in
  [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
  [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
  Simulator fixtures do not establish real Walrus certification or provider
  availability.
