# Unattended reciprocal PR coordination

SF-040 adds a local WSL service in `tools/pr-coordinator`. Codex reviews Claude's PRs; Claude reviews Codex's. The service polls GitHub, records jobs and review receipts in `~/.local/state/pr-coordinator/reviews.sqlite3`, launches the existing installed CLIs and serially integrates approved changes. GitHub login is not model identity: implementation families and branches come from atomic `ticket` ownership, and reviewer families come from independently launched worker jobs.

The committed engine, installer, tests and systemd unit can be used in another registered repository. Local configuration selects each project ID, canonical root, repository and GitHub account. Git uses that checkout's existing SSH alias. Tokens are read from `gh auth token --user ACCOUNT` into the individual command environment; they are never stored in configuration or printed. The example enables SuiFleet only; add a second project entry for Vortech with repository `therealtime/vortech`, project `vortech`, its registered root and GitHub account `therealtime`.

## Install and operate

```bash
python3 -m unittest discover -s tools/pr-coordinator/tests -v
python3 tools/pr-coordinator/install.py
# Read-only scan: no workers, reviews, pushes, merges or ticket mutations.
python3 ~/.local/lib/pr-coordinator/coordinator.py \
  --config ~/.config/pr-coordinator/config.json
# User-authorized continuous operation:
systemctl --user enable --now pr-coordinator.service
systemctl --user status pr-coordinator.service
journalctl --user -u pr-coordinator.service -f
systemctl --user stop pr-coordinator.service
```

The installer resolves absolute Codex/Claude paths and the Node directory, so a systemd service does not depend on an interactive nvm shell. It preserves an existing configuration. Default workers inherit their CLI's configured model and account/subscription; they report the actual model in the receipt. Codex uses its supported noninteractive [`codex exec` mode](https://developers.openai.com/codex/noninteractive); Claude uses print mode. Both receive the user's authorized unattended permission settings. Review/fix jobs consume model usage. Authentication or usage errors are recorded, never mistaken for approval.

WSL must be running for this local service to poll. The user service restarts with the WSL systemd user session; it cannot wake a powered-off Windows host. `KillMode=process` lets existing workers finish if the daemon restarts. Their wrapper persists PID/start time and actual exit status, so the restarted service can collect completed results without launching another worker. Stopping the service stops new work; it does not interrupt an already-running review or feedback repair. Inspect the recorded PID before explicitly stopping a worker.

## Live agent dashboard (SF-106/107)

`fleet-status` (also available as `agent-dashboard`) shows what each worker is doing right now: project, family and
model, implementation/review/repair role, ticket, PR, elapsed time and the ticket
title with a short task summary. It also shows configured capacity and use,
waiting/retry/CI/blocked/retained states, the ticket queue, live resource locks,
how fresh each local store is, and what changed since the last refresh.

```bash
python3 tools/pr-coordinator/install.py --dashboard-only   # copies one file and both launchers; no service restart
fleet-status                         # live view on a terminal: q/Esc quit, r refresh, ↑/↓ j/k PgUp/PgDn scroll
fleet-status --project suifleet --interval 10
fleet-status --once               # one readable snapshot (default when stdout is not a terminal)
fleet-status --json               # one machine-readable snapshot
```

The full installer also installs it. `--dashboard-only` copies only `dashboard.py`
and writes `~/.local/bin/fleet-status` and `~/.local/bin/agent-dashboard`.
An obsolete prototype symlink is replaced without modifying its target. It leaves the coordinator, dispatcher,
worker, configuration and systemd units alone, and running workers keep running.
`--live` exits with status 2 when stdout is not a terminal. One-shot modes exit 3
when any store is unavailable.

Each refresh reads the atomic ticket database, `dispatch.sqlite3`,
`reviews.sqlite3`, the private configuration, and each PR's latest
`snapshot.json`/`process.json` receipts. Every database is opened `mode=ro` with
`query_only`. Normal dashboard refresh makes no network calls and starts no GitHub, ticket or
model subprocess. The explicit `--refresh-github` option reads GitHub once before
viewing; it never enables network polling in the live loop. It only selects whitelisted columns and fields: claim tokens, lock
tokens, claim blobs, prompts, PR bodies/comments and worker logs are never read
into its output. Handoff evidence and job errors are shortened, and token-like
strings in them are redacted.

The opening summary separates three different measures:

- **GitHub open issues / open PRs:** complete paginated REST inventories, including
  draft PRs and PRs without active tickets. PRs are excluded from the issues count.
  These are cached GitHub counts with their age; after 30 minutes they show STALE.
- **Tracked open tickets:** the atomic database's backlog + in progress + review +
  blocked counts. Completed tickets appear separately in local history. Differences
  from the GitHub open inventory list the issue numbers or ticket keys to investigate.
- **Running workers:** PID-verified activity on those issues and PRs. Never add this
  number to the ticket total. Several workers can work on one PR over time.

Refresh the GitHub inventory explicitly when needed:

```bash
fleet-status --project suifleet --refresh-github --once
```

This uses each configured project's `repository` and `github_account`, two
paginated REST listings and a 60-second timeout per command, avoiding GraphQL.
It writes only sanitized number/title/draft fields and a timestamp under
`~/.local/state/pr-coordinator/github-inventory/PROJECT.json` (honoring
`XDG_STATE_HOME`). Both lists must succeed before replacing the snapshot. Failure
returns status 3 and preserves the previous cache. Missing, corrupt or wrong-repo
snapshots show **unknown**, never zero. Plain live refresh and `r` remain offline;
repeat the explicit command to update GitHub counts. PR associations use ticket keys in PR titles or existing coordinator receipts.
This is a display association, not a claim of ownership or merge eligibility.

A worker counts as **active** only when its recorded PID is running, has the
recorded `/proc` start time and is not a zombie. Other cases are listed separately
and never counted active:

| State | Meaning |
|---|---|
| `stale` | The process exited or the PID was reused, and the dispatcher hasn't reconciled it yet |
| `lingering` | The wrapper exited but a process in its session is still running, so it still holds capacity |
| `unverified` | There is no readable process identity |
| `manual` | A claim with no worker receipt |
| `orphaned`, `quarantined`, `parked` | Reservations kept for explicit recovery |

Capacity uses the same buckets: *held* is unverified or waiting work that still
occupies a slot, and *retained* is parked or quarantined work. Review tickets show
CI as `ci-failed`, `ci-pending` or `awaiting-review`, taken from the newest local
PR snapshot and its age. These are local receipts, not a live GitHub query.

If a store is missing or corrupt, the dashboard reports it as `UNAVAILABLE`, never
as an empty queue. Duplicate open jobs for one PR, and several retained receipts for
one ticket, appear as warnings. Small terminals scroll, and narrow ones drop the
model and project columns.

## Ownership and review loop

Only same-repository PRs whose head branch exactly matches a centrally reserved ticket are dispatched. Unknown ownership, draft PRs, blocked/in-progress tickets and missing model identity are reported and left alone. An active implementation is never resumed by a second worker. A feedback worker can resume only its existing family's published Review claim, with its unchanged token and clean worktree. The coordinator renews a running feedback claim; workers must also assert/heartbeat before edits and commits.

SQLite transactions reserve one job per PR and enforce configurable per-family review and repair limits across configured projects. Each category defaults to one worker. Reviews use an isolated detached worktree of the fetched exact head. They read all issue comments/formal reviews and review threads plus the ticket acceptance/evidence. A thread exceeding the implemented 100-comment limit stops the scan, rather than silently truncating feedback. No path or hardware is reserved for development. Workers acquire shared build/port/hardware resources only when actually using them, under the existing rules.

The worker writes a structured result containing its verdict, actual model, acceptance completeness and exact verification evidence. The engine publishes that real result on the PR, optionally using the configured independent GitHub reviewer account for a formal review at the specific commit. A changes-requested result queues the implementation owner's repair worker. Repairs push scoped Tyler-authored commits, reply to feedback and return the ticket to Review; the changed head is then independently reviewed again.

A reviewer must check existing change requests and acceptance, not infer approval from CI. Missing hardware/live evidence required by the ticket produces a blocked result. Worker mode does not authorize contract publication, deployment, hardware programming, issue creation or recursive worker launches. An expired ownership token is never recovered automatically.

## Merge gates and completion

The service requires:

- Open, ready, mergeable PR with published Review ownership and no unresolved threads.
- Approval from the other model family for the exact head, base commit and current feedback snapshot, with complete acceptance evidence.
- Every reported CI/status check explicitly successful; missing, pending, failed, skipped or cancelled checks block integration.
- No remaining formal GitHub change request from any reviewer.

Independent reviews wait until every reported check succeeds and any required testnet status succeeds for the exact integration base. Pending, absent, failed, skipped or cancelled checks leave the PR waiting; actionable change requests and base-integration repairs still run. Review fingerprints include normalized check outcomes and required testnet state/base evidence, so a completed check automatically invalidates a stale blocked review and requests a fresh independent decision. Reordering checks or changing display timestamps, URLs or IDs alone does not trigger another review. A genuine acceptance blocker remains blocked until new evidence or scope changes.

A branch must include the current integration base before independent approval; otherwise its existing owner is automatically asked to merge/rebase and rerun checks. Changed head/base/comments/reviews invalidate a receipt. Coordinator-generated publication receipts do not cause an infinite review loop. Gates are refreshed before preparing a merge and again before push. Integrations use a clean detached base worktree and a local merge commit with Tyler Bush's verified author and committer identity. They use a normal fast-forward push; a competing main update is rejected, never overwritten. Another actor can still update a PR during a network operation: the engine verifies GitHub's merged state and exact head before completing the ticket. Cooperative workers must leave merges to this single coordinator; repository branch protections remain independently applicable.

Integration intent is recorded before push, and completion is retried after a crash or tracker mirror error. A prepared commit absent from remote main retries the guarded push; an already included commit awaiting GitHub’s merged notification waits for reconciliation without dispatching another worker. Individual PR failures do not abort other PRs/projects. Completed clean scratch worktrees are removed without force; dirty diagnostics remain available. Automatic integration requires matching complete acceptance. Verified merges are marked Done through `ticket complete` under the merge-closes-issue policy below; external merges do not establish missing live or hardware evidence.

## Failures and recovery

Inspect `journalctl` and the local SQLite `jobs` table; each job directory contains its snapshot, prompt, worker log, structured result, process identity and exit receipt. Logs are local evidence, not files to upload wholesale. Claim tokens remain local and are not included in the published review snapshot.

A stopped review CLI with a durable exit receipt retries usage/auth failures after exponential backoff (5–60 minutes), retaining its logs and reservation history. An interrupted launch, invalid feedback-worker result or disappeared worker is retained as failed/orphaned; it does not restart an unknown implementation or clear a ticket reservation. Recovery requires checking that the original PID/start-time process has stopped, checking the owner worktree/claim, and recording a coordinator/ticket handoff before retrying. Never delete the central ticket database or release another live worker's claim. Operational interruptions therefore fail closed; normal review/change-request/repair/approval cycles require no human reminder.

## Merge closes the implementation issue

Under the user's merge-closes-issue policy, the service adds `Closes #N` to each owned PR using the ticket's verified same-repository issue URL. `Refs #N` alone does not close an issue. After verifying a default-branch merge and its commit ancestry, reconciliation marks the atomic ticket and Project Done, explicitly closes and verifies the GitHub issue, and retries partial failures. Periodic discovery also imports merges performed outside the service, so missed notifications and older merged PRs are reconciled. Unknown ownership, other repositories and unmerged/closed-without-merge PRs never trigger closure. Active implementation claims require their owner handoff before coordinator completion.

Automatic merging still requires the existing independent-review, CI and acceptance gates. A verified external merge is treated as implementation completion under this policy, without inventing approval or test results. Any remaining live-wallet, public-network or hardware validation must remain visible in an existing acceptance ticket or explicit follow-up. SF-003/#6's merged frontend implementation is closed; its remaining real-extension acceptance is tracked in SF-010/#11 and SF-031/#32.

## Automatic implementation dispatch

The optional `ticket-dispatcher.service` starts the next highest-priority eligible
implementation without a chat prompt. It uses the installed atomic `ticket` CLI;
it does not assign tickets by changing GitHub Project fields. PR review, feedback
repairs, gated integration and issue closure remain the PR coordinator's job.

Configure explicit slots in the existing private
`~/.config/pr-coordinator/config.json`, alongside `workers` and `projects`:

```json
"implementation_slots": [
  {"project": "suifleet", "family": "Codex", "model": "gpt-6.1-sol", "max_workers": 2, "enabled": true},
  {"project": "suifleet", "family": "Claude", "model": "claude-opus-5-5", "max_workers": 2, "enabled": true}
]
```

Use the actual installed worker model; these are recorded identities, not model
selection flags. Workers must report their actual running version in evidence.
Use one configuration entry per project/family. Its `max_workers` is an integer from 1 to 16 and defaults to 1 for existing configurations. New installations have no enabled slots. Each worker still receives a distinct atomic claim, session and worktree.
Other repositories require their own registered atomic namespace, explicit project
configuration, GitHub account and existing SSH alias; nothing enables Vortech
implicitly. Commands are argv arrays, never shell-expanded PR or issue text.

```bash
python3 tools/pr-coordinator/install.py --enable --enable-dispatcher
systemctl --user status ticket-dispatcher.service
journalctl --user -u ticket-dispatcher.service -n 30 --no-pager
python3 ~/.local/lib/pr-coordinator/dispatcher.py \
  --config ~/.config/pr-coordinator/config.json --status
```

For dispatcher-only changes, add `--dispatcher-only` to preserve the installed
review coordinator and worker. Concurrency changes to both engines require the full
installer and restarting both services; their existing workers survive the restart.

The service polls every 60 seconds, with durable exponential idle/pre-claim failure
backoff from 60 to 900 seconds. A failed fetch before a claim is retried; uncertain
claim or worker failures still retain ownership and stop the slot. A process lock serializes scans; durable private
SQLite receipts are in `~/.local/state/pr-coordinator/dispatch.sqlite3`, with worker
logs and process/exit receipts under `implementations/`. `--status` prints no tokens
and performs no GitHub operations. Without `--execute`, a scan performs no actions.
Tests run offline with `python3 -m unittest discover -s tools/pr-coordinator/tests`.

Manual active or blocked claims without durable worker identity still consume implementation capacity, including expired reservations. Managed claims count once alongside their dispatcher records. Review and repair categories have separate capacity; existing workers are not preempted.

A worker starts only after refresh, fetch, atomic claim, published ownership and assert. It inherits the configured project's account token and claim, heartbeats, pushes scoped user-authored commits, and opens a linked PR. A successful stopped `review_ready` worker with published Review/Done ownership frees its implementation slot. Reconciliation revisits orphaned records after restart, so a delayed valid handoff is not permanently stranded.

Verified stopped blocked workers are **parked**. Verified stopped jobs without a valid handoff are **quarantined**. Both retain ticket ownership, branch, logs and evidence, but no longer consume execution capacity for unrelated tickets. Quarantined review/repair jobs retain exclusion on their original PR. Neither state authorizes automatic claim release, duplicate implementation or retry of an uncertain mutation. Explicit recovery still requires inspection and the atomic ticket workflow. Implementation slots stop claiming when their combined parked/quarantined count reaches `max_quarantined` (default 3, integer 1–16). Quarantine also applies persistent project/family exponential backoff (60–900 seconds), reset only by a valid completed handoff, never merely by starting a process. Review/repair reservations stop after three quarantined jobs per family/category across projects; each quarantine adds durable exponential backoff (300–3600 seconds). These retention caps require explicit operator recovery; they bound a systemic fast-failure cascade while allowing limited unrelated progress. The diagnostics are visible in dispatcher `--status`, the private review-job database and service journal.

Stopped means the recorded PID/start-time wrapper has exited **and** a Linux `/proc` scan finds no non-zombie process in its worker session. A surviving model subprocess in that session keeps the slot occupied. A subprocess that deliberately daemonizes or uses `setsid` can escape this session scan; workers must not detach subprocesses. This is cooperative process tracking, not cgroup containment. Missing process identity, unreadable process state, interrupted claims and ambiguous launches fail closed. A malformed dispatcher receipt is isolated to its own job rather than aborting all reconciliation. Capacity counts running, launching and uncertain jobs against configured limits; it does not count verified parked/quarantined jobs. An idle slot can still indicate no dependency-eligible ticket, a retry backoff, or an unverified manual reservation; it is not permission to bypass dependencies or fabricate work.

Set a slot's `enabled` to `false` and restart the service to prevent its next claim,
or run `systemctl --user disable --now ticket-dispatcher.service` to stop dispatching.
Existing workers survive service stops; inspect them before any recovery. User
services run while this WSL instance is running; this does not keep Windows or WSL
awake. Watch service status and receipts for authentication, quota and external
acceptance blockers. Automated dispatch does not grant public-chain deployment,
physical hardware programming, release credential provisioning or issue-creation
authority to another model.

Implementation acceptance follows [the user-authorized split](IMPLEMENTATION_ACCEPTANCE.md). Its configured scope is included in each review fingerprint and worker snapshot; changing it requires fresh independent review and does not bypass source findings or CI. Claim tokens are stored only in the private local dispatcher SQLite database to allow restart ownership checks; GitHub tokens are never persisted there. Review and implementation slots are separate, so one family may briefly run an implementation and a review worker together.

The optional top-level `review_worker_limits` config maps each model family to
`review` and `fix` limits (integers 1–16, each defaulting to 1):

```json
"review_worker_limits": {
  "Codex": {"review": 1, "fix": 2},
  "Claude": {"review": 1, "fix": 2}
}
```

A lengthy image repair can therefore run alongside an independent repair without
occupying the read-only review category. At most one active job may own a given PR,
regardless of family or category. Orphaned jobs retain one position until explicit
recovery. Lowering any limit or disabling dispatch only prevents new launches; it
does not interrupt existing workers. Merges remain serialized by the coordinator.
These are process limits, not permission to parallelize use of a locked build,
network or hardware resource. Review and repair workers consume memory and model
usage too; choose bounds suited to the host.

Public Sui testnet testing is user-authorized for this project through explicit private project configuration:

```json
"testnet_authorization": {
  "network": "testnet",
  "chain_identifier": "4c78adac",
  "wallet": "/home/tyler/.config/suifleet/testnet/agent/client.yaml",
  "lock_resource": "network:suifleet-testnet"
}
```

This grant is absent by default and validated before claiming/launching. Worker prompts permit planned testnet publication/upgrades and test-gas funding, require chain/account/authority verification and coordinated machine mutation locks, and retain the mainnet/production/hardware boundaries. Account setup and the live runner are supplied by [PR #63](https://github.com/tyler-suifleet/suifleet/pull/63), tracked in [SF-010](https://github.com/tyler-suifleet/suifleet/issues/11). Do not duplicate Claude's active testnet run or expose wallet contents. CI still follows its configured environment controls; this does not provision CI secrets or production signing credentials.

### Transient transport failures and scoped repairs (SF-093)

Known read-only GitHub operations, `git fetch`/`ls-remote`, and idempotent ticket
refresh have at most three attempts with bounded backoff and read timeouts.
Authentication/permission failures remain actionable; unknown mutation outcomes
are reconciled rather than blindly replayed. A missing PR snapshot never becomes
an approval or empty feedback. Failed snapshots, worker collection and merged-PR
discovery items are deferred independently so healthy PRs keep progressing.

The standalone ticket engine also retries individual GitHub snapshot reads;
install its reviewed `ticket_transport.py` version alongside the coordinator
repair. Refresh still imports only a complete snapshot and preserves reservations
on failure. A successful refresh is required before dispatching new work.

For a coordinator repair while the installed dispatcher has a newer independent
fix, use `python3 tools/pr-coordinator/install.py --coordinator-only`, then restart
`pr-coordinator.service` and `ticket-dispatcher.service` so both import the updated
shared read helper. This preserves worker/dispatcher code, service units, private
configuration and databases. Back up the previous installed files and record
hashes/tests before replacement; restore those files and restart parents to roll
back. Parent restarts preserve existing worker sessions under `KillMode=process`;
never clear reservations or restart uncertain workers to make capacity appear free.

### Feedback launch publication recovery

Before resuming a Review claim, the coordinator persists launch intent in its
SQLite job row: claim-token hash, ticket, branch, worktree and exact PR head.
If resume changes local ownership but its Project mirror fails, the job retains
its reservation and retries after five minutes. Recovery reads durable ownership,
requires matching intent and a clean branch at that head, publishes any pending
revision, and asserts the claim. It does not repeat resume for an In progress claim.
Recovery uses fresh base, feedback and checks while retaining the reserved receipt
directory and its original feedback reservation key. Fresh snapshot feedback is
used by the worker without rewriting that key, avoiding collisions with another
reserved or completed job. A changed head or ownership, dirty
checkout, PID or worker receipt stops this path for explicit inspection; arbitrary In progress claims never authorize launch.

Immediately before spawning the worker the coordinator clears recoverable intent.
An interruption beyond this boundary without a process receipt is uncertain and
requires manual recovery; it cannot automatically launch a duplicate worker.
Existing worker receipts continue through normal process-identity reconciliation.
This repair changes only coordinator code/database schema, not dispatcher or
ticket-tool behavior. Use the coordinator-only installation/backup/rollback steps
above after independent review; this feedback worker does not install services.

### Pending Review ownership publication

A Review claim whose local revision is ahead of its Project mirror
(`publish_pending`) previously stayed unscheduled until someone ran `ticket
publish`. A normal `--execute` scan now retries that recoverable mirror failure
before evaluating the PR, for an owned open PR only:

- Re-read the claim with `ticket show` and require the listed token, agent,
  model, session, branch, worktree, revision and Review status. Any change defers
  that PR to the next scan without publishing. In progress, blocked or expired
  states are never resumed, released or published by this path.
- Run the supported `ticket publish KEY` (300-second timeout; one in-scan retry for
  transient network failures), then re-read the claim. Scheduling uses only that
  fresh published owner, and every existing head/base/feedback, review, CI and
  merge gate still applies. A revision or ownership change during publication
  defers scheduling.
- On failure, the claim and pending state are untouched. The
  `ownership_publication` table in `reviews.sqlite3` records the claim-token hash,
  revision, attempt count, next retry (60 s doubling to at most one hour) and a
  sanitized reason (`timeout`, `transient (exit N)`, `failed (exit N)`, `GitHub
  rate limited` or a known `ticket` refresh message). Logs and the table never
  contain argv, stderr, or tokens. A new claim or revision restarts the schedule.
  Other PRs in the same scan continue normally.

Dry runs never publish. Inspect a stuck claim through the coordinator journal
and a read-only query of `ownership_publication` (`project, ticket, revision,
attempts, retry_after, error`); `ticket --project suifleet publish KEY` remains
the manual path.

### Reviewing while CI runs

The coordinator starts independent source review for an owned, published Review
PR as soon as its current base is integrated and GitHub reports it mergeable.
Drafts, unknown mergeability, unpublished ownership and pending integration
intents do not bypass their existing gates. Required CI/testnet checks can run
in parallel with source review.

A snapshot with missing, pending or unsuccessful checks is **source review
only**. Workers report concrete code defects immediately, or return blocked
with their completed source-review evidence. Collection enforces incomplete
acceptance even if a worker mistakenly approves that snapshot. Pending/failed
check transitions share a source-review key to avoid duplicate reviews. Changes
to head, current base, comments, reviews, threads or acceptance scope still
invalidate it. When all checks succeed, the full check evidence changes the
fingerprint and dispatches a final independent evidence review, with the prior
review available as evidence to revalidate. No review is automatically promoted.
Only a complete, published, current-snapshot approval can pass the unchanged
mandatory CI and exact-base testnet merge gates. Blocked reviews publish a
GitHub comment, not a code change request; genuine code findings still request
changes.

PR reads no longer request comments/reviews twice: complete paginated REST
collections and complete GraphQL review threads remain authoritative. Only
content-addressed Git trees and SHA-to-SHA ancestry comparisons are cached;
branch tips, PR state, feedback and CI are freshly read at integration gates.
An actual account rate limit records a persistent, account-specific shared
backoff (60–3600 seconds, default 300) rather than retrying each PR. No API
account switching is used to evade limits. Failed/incomplete snapshots fail
closed and remain visible in the coordinator's per-PR triage output.

### Obsolete implementation receipts (SF-110)

Before checking implementation capacity, the dispatcher revisits parked and
quarantined receipts. It archives only a verified stopped wrapper with no
surviving session members and no reused PID, after reading current ownership.
Missing or unreadable identity, pending Project publication, unknown ownership
and unresolved current quarantines remain reserved. These checks also apply
when retained receipts are counted against execution capacity.

Retirement requires published Done, an explicit release/recovery event after
the exact historical claim generation, a same-claim published Review handoff,
or a verified newer same-claim feedback repair. New repair launches preserve a
hashed claim/branch/worktree/head proof in `jobs.repair_claim`; historical jobs
without this proof cannot establish replacement. A repair must have a matching
process receipt and either be running under its recorded identity or have a
successful collected `fixed` result.

The dispatcher locks the central atomic-ticketing database with `BEGIN IMMEDIATE`,
compares the fresh ownership revision and publication revision to its observation,
then compares the original dispatch row and rechecks stopped process identity
inside a dispatch transaction. It writes only dispatcher receipts: original
status/message and a sanitized reason go into `retired_runs`, while the retained
run becomes `finished`. Original claim, directory and process records remain.
It never changes ownership tokens, branches, worktrees or GitHub state. Each
failed reconciliation is isolated and retried on a later scan; quarantine limits
and retry safeguards are unchanged. A crash before receipt commit leaves the
reservation; a crash after commit leaves a durable retirement record.

This requires the existing central ticket database schema; missing/incompatible
stores fail closed. Install coordinator and dispatcher together after review so
new repair proof can be recorded. Existing unknown receipts require explicit
operator inspection, not database deletion or threshold increases. No services
are installed or restarted by the implementation tests.

### Reviewed local runtime activation (SF-113)

After this implementation is independently reviewed and merged, bootstrap once
**from the exact fetched main revision**, using the existing installed private
project configuration:

```bash
python3 tools/pr-coordinator/install.py --activation-project suifleet
systemctl --user status runtime-activation.timer runtime-activation.service
python3 ~/.local/lib/pr-coordinator/activation.py --status
journalctl --user -u runtime-activation.service -n 30 --no-pager
```

Bootstrap enables a two-minute timer and changes only the parent units' runtime
paths. It preserves their existing environment, protections and `KillMode=process`.
Bootstrap also requires existing integration evidence and an independently published
exact-head/base approval plus matching remote merge evidence. It stages/tests
committed main before stopping parents and restores their original
units on startup failure. Run it only after review/merge; an implementation worker
must not install its unmerged branch. Existing model workers continue running.
The existing full/scoped engine-copy installer refuses to bypass an enabled
activation installation. Dashboard-only installation remains separate.

The fixed bootstrap supervisor serializes activation with `activation.lock`.
It fetches main directly from the configured GitHub repository, checks installed
ancestry and examines every first-parent runtime change since the installed
revision. Its allowlist is exactly `coordinator.py`, `dispatcher.py`, `worker.py`.
For each change it requires the coordinator's durable `activation_merges` receipt,
matching two-parent merge ancestry, a published completed independent approval,
and a fresh GitHub merged PR with the same repository, head and merge commit.
The coordinator writes the receipt only after its existing final review/ownership/
CI gates, before the guarded push. A receipt alone cannot authorize an unmerged
commit. External/direct runtime merges without this proof fail closed and need
explicit operator provenance inspection; they are never silently installed.

Files are read by immutable Git revision, compiled, and tested with that revision's
complete offline coordinator suite in a temporary source tree. Checkout files,
private credentials, model configuration, wallet protections and system units
are not update payloads. `runtime-compatibility.json` must retain protocol/store
compatibility version 1; incompatible migrations require explicit reviewed
bootstrap work. Preserve backward-compatible durable schemas when using version 1.
The fixed supervisor, bootstrap installer and unit changes require explicit
reviewed maintenance; they cannot update their own trust boundary through this
runtime allowlist. This grant deploys no application, contract, daemon firmware
or hardware and changes no model permissions.

A generation is fully staged/fsynced before replacement. Main is fetched again
before switching. Parents are stopped with their worker-preserving units, the
single `current` symlink is atomically replaced, and only previously active
parents are started. Readiness must match the unit's actual PID, Linux start
identity and generation after lock/store initialization, within 30 seconds.
Existing claims, SQLite stores, worker directories, PID/start identities and
actual wrapper exit receipts stay in place; the normal parent adoption logic
reconciles them. A rollback uses the original generation and active-unit set;
startup or replacement failure restores that pointer. Durable pending intent
also rolls back on the next timer run after supervisor interruption. Rollback
failure leaves pending intent and the service failed for explicit recovery;
never clear claims or worker receipts to repair activation.

`activation.py --status` reads local receipts without network calls and reports
`installed`, last observed `merged`, and activation state. Identical runtime
bytes record `unchanged` without restarting parents. Failed validation/provenance
records a sanitized diagnostic and retries on later timer runs. A startup rollback
pins that failed target against automatic retry. Inspect the private
`~/.local/state/pr-coordinator/activation.json`, fix the reported problem, and
archive/remove that receipt only after confirming parent/worker identities and
the intended rollback target. Never delete the ticket or job databases.
Generations are retained for inspection and rollback; no automatic garbage
collection removes code referenced by an existing worker. The supervisor and
both parents must use the same default state directory for readiness receipts.

The SF-113 fixture demonstrates successful local activation and continued real
worker execution/exit receipt; it does not claim the unmerged implementation
was installed into the running services. Independent review and exact-head CI
remain required before operational bootstrap.
