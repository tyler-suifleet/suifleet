# Staged rollouts (SF-017)

The wallet's Deployments → Staged rollouts page creates schema-3 deployments
from an authenticated recipe blob and both SHA-256 digests. Enter ordered cohorts
(one line per cohort), concurrency, grants per rolling window, bake duration,
minimum samples, inclusive failure threshold in basis points and stall timeout.
Cohorts are immutable, disjoint snapshots of at most 512 direct device targets.
Each cohort must initially contain at least the minimum sample count.

The deployment creator coordinates by signing **Verify current health and
advance** transactions. This is operator-driven coordination: no background
service or unattended wallet signer is installed. Closing/crashing the browser
stops ticks; reopening and selecting the record resumes from on-chain state.
Every grant, outcome, skip, cohort, bake timestamp, pause/abort and revision lives
in the `RolloutKey` dynamic field. `RolloutDecision` events record each tick's
reason, grants, state, cohort, time and revision. Manual pause/resume/abort and
skips require an audit reason. The transaction inputs and updated snapshot retain
which IDs were skipped. Only never-admitted members can be skipped.

An advance grants only online members of the current cohort, subject to both
outstanding-grant concurrency and a rolling time-window rate limit. Offline
admitted members retain their slot: timing out never proves an updater stopped.
A stalled cohort pauses after the configured timeout. Restore connectivity and
resume, skip never-admitted offline members, or abort permanently. Skipping too
many members to reach the minimum sample intentionally prevents progression;
create a new rollout rather than weaken the policy.

Current device identity-bound telemetry is verified by the existing telemetry
client. Configure its read token on Devices first. Reports must be at most
120 seconds old, not from the future, and identify this deployment’s exact root component name/version as installed
after its admission timestamp to supply an applied health sample. Unavailable,
unverified, offline or missing reports never pass health gates. The creator's
transaction attests the online/healthy/degraded lists; Move does not verify
off-chain telemetry signatures. The creator is trusted to attest honestly.

The package records outcomes only through its grant-checked, active-identity
reporting path. Older callable packages can write ordinary deployment receipts,
but those receipts cannot settle rollout grants. Failures and observed degraded
applied health are sticky. Across admitted members, once the minimum sample is
met, `failures * 10000 >= samples * failure_bps` permanently aborts the rollout.
Below that threshold, a cohort must settle all non-skipped members and provide
fresh health for every applied member before baking. Every tick during baking
must pass the health check; a missing sample resets the bake. This is sampled
health, not proof of continuous availability between ticks. After the bake,
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
the introduction origins of `RolloutKey`, `Rollout`, `RolloutPolicy` and
`RolloutDecision`, while preserving older types' origins. No existing objects
are automatically converted to staged rollouts. Delete schema-3 records through
`delete_rollout`, which removes the new dynamic field before existing cleanup.

- `make test-contracts`: rate/concurrency/bake, minimum-sample and sticky abort,
  offline capacity and creator authority tests alongside existing regressions.
- `make check-interfaces`: compiled ABI, BCS, rollout transaction/health validators.
- `make edge-test` and `make test-ota`: real daemon regressions, including
  `scripts/test_rollout.py` with three daemon cohort members, offline/restart,
  pause/resume, read outage and abort behavior.
- `scripts/test_rollout_localnet.py`: run with an indexed throwaway node/faucet
  inside Docker `--network none`; real base→head upgrade, existing objects,
  new type origins, old-package receipt attack, grant-checked reporting,
  minimum-sample failure abort and cleanup, with execution effects checked.

Physical Pi/target acceptance remains [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and was not established by these tests. Normal current-head CI and automatic
public-testnet checks remain implementation gates. No production rollout or
physical programming is authorized by implementation acceptance.
