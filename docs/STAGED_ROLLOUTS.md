# Staged rollouts (SF-017, SF-138)

The wallet's Deployments → Staged rollouts page creates schema-3 deployments
from an authenticated recipe blob and both SHA-256 digests. Enter ordered cohorts
(one line per cohort), concurrency, grants per rolling window, bake duration,
minimum samples, inclusive failure threshold in basis points and stall timeout.
Cohorts are immutable, disjoint snapshots of at most 512 direct device targets.
Each cohort must initially contain at least the minimum sample count.

The deployment creator coordinates by signing **Verify on-chain health and
advance** transactions. This is operator-driven coordination: no background
service or unattended wallet signer is installed. Closing/crashing the browser
stops ticks; reopening and selecting the record resumes from on-chain state.
Every grant, outcome, skip, cohort, bake timestamp, pause/abort and revision lives
in the `RolloutKey` dynamic field. `RolloutDecision` events record each tick's
reason, grants, state, cohort, time and revision. Manual pause/resume/abort and
skips require an audit reason. The transaction inputs and updated snapshot retain
which IDs were skipped. Only never-admitted members can be skipped.

An advance attempts grants to members of the current cohort, subject to both
outstanding-grant concurrency and a rolling time-window rate limit. Offline
admitted members retain their slot: timing out never proves an updater stopped.
A grant does not prove the device is online or has started work. A stalled cohort
pauses after the configured timeout. Restore connectivity and
resume, skip never-admitted offline members, or abort permanently. Skipping too
many members to reach the minimum sample intentionally prevents progression;
create a new rollout rather than weaken the policy.

The gate reads **device-signed on-chain post-apply health**, without an HTTP
indexer, telemetry read token, browser wall-clock freshness window or background
server. After an effects-checked outcome receipt, the daemon immediately submits
`report_rollout_health` using its active device capability. Its judgement requires
this deployment in the confirmed installed inventory, readable journal/supervisor
state, running supervised components and healthy enabled services. Unconfirmed or
indeterminate installs and known service problems cannot produce a healthy sample.
This is device-reported application health, not boot confirmation or firmware safety.

The report is stored under this deployment's `RolloutHealthKey`. Move requires a
current active identity, a non-skipped grant and a settled grant-checked outcome;
it stamps chain time and retains the reporting signer. The transaction signature
and capability authenticate the sample; no off-chain signature attestation by the
browser is needed. There is one immutable sample per member (at most 512 per
rollout). Repeating a call is idempotent and cannot replace degraded health with
healthy health or grow storage. This separate, bounded deployment signal is exempt
from the periodic `fleet_data` cadence: daily health (or no periodic producer)
does not delay it or cause extra periodic writes. Health freshness means **after
this deployment's grant and outcome**, rather than within 120 wall-clock seconds.

The daemon journals health completion separately from the ordinary receipt, retries
uncertain execution after restart, and never replays the updater to get a sample.
Older journals queue their missing sample once on upgrade; deleted historical
records and non-staged deployments require no health write. A healthy sample must
still belong to an active signer at advance time. Revocation/rotation fails closed;
a superseded sample cannot be replaced, so use an audited new rollout if another
sample is needed. A missing report holds the gate and the existing stall timeout
still pauses the rollout.

`advance_rollout_onchain` derives health lists inside Move. The browser supplies
grant candidates, not assertions of recent device liveness. Offline candidates can
receive grants and retain capacity until they report; concurrency, rolling rate,
stall timeout and manual skip/recovery rules still apply. This avoids depending on
rapid periodic telemetry simply to admit the first cohort. The original
creator-attested `advance_rollout` entry point remains callable for compatible
upgrades; the deployment creator remains trusted and can use that legacy path.
The shipped client uses only the new on-chain gate.

The package records outcomes only through its grant-checked, active-identity
reporting path. Older callable packages can write ordinary deployment receipts,
but those receipts cannot settle rollout grants. Failures and observed degraded
applied health are sticky. Across admitted members, once the minimum sample is
met, `failures * 10000 >= samples * failure_bps` permanently aborts the rollout.
Below that threshold, a cohort must settle all non-skipped members and provide
a healthy post-apply sample for every applied member before baking. Every tick during baking
must still pass the on-chain sample and active-identity check; a missing sample
resets the bake. The immutable post-apply sample does not establish continuous
availability or detect a later runtime regression between ticks. After the bake,
the next cohort becomes eligible for admission on a subsequent tick.

The daemon independently checks the creator's current targeting authority and
schema-3 grant, including a fresh read after downloads before disruptive work.
Missing/malformed policy and transport errors fail closed. Pause/abort prevents
new applies; an admitted apply already executing may finish and report truthfully.
Terminal abort observations and deferred events survive daemon restart. The
rollout controller never reboots a device or supplies platform rollback logic.

## Upgrade and validation

This is an additive package upgrade: existing record layouts/signatures remain
unchanged. Schema 1 and catalog schema 2 keep their existing paths; older clients
reject schema 3 before apply. Regenerate manifests for the new clients: record
the introduction origins of `RolloutKey`, `Rollout`, `RolloutPolicy`,
`RolloutDecision`, `RolloutHealthKey` and `RolloutHealth`, while preserving older
types' origins. Health types form the all-or-none optional `rollout_health`
manifest group: historical manifests still restore, while the new gate refuses
a manifest without this group. Existing rollout objects acquire the new health field lazily; no
layout migration is needed. No existing objects
are automatically converted to staged rollouts. Delete schema-3 records through
`delete_rollout`, which removes the new dynamic field before existing cleanup.

- `make test-contracts`: rate/concurrency/bake, minimum-sample and sticky abort,
  offline capacity and creator authority tests alongside existing regressions;
  missing/grantless health, immutable retries, revocation, degraded abort and
  grant-relative freshness beyond 120 seconds.
- `make check-interfaces`: compiled ABI, BCS, rollout transaction/health validators.
- `make edge-test` and `make test-ota`: real daemon regressions, including
  `scripts/test_rollout.py` with three daemon cohort members, post-apply health, offline/restart,
  pause/resume, read outage and abort behavior.
- `scripts/test_rollout_localnet.py`: run with an indexed throwaway node/faucet
  inside Docker `--network none`; real base→head upgrade, existing objects,
  new health type origins with existing rollout objects, old-package receipts
  without health, grant-checked reporting,
  minimum-sample failure abort, real-daemon post-apply health and next-cohort
  admission with a real daily-cadence fleet-data writer and no HTTP indexer,
  and cleanup, with execution
  effects checked.

Physical Pi/target acceptance remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and was not established by these tests. Normal current-head CI and automatic
public-testnet checks remain implementation gates. No production rollout or
physical programming is authorized by implementation acceptance.
