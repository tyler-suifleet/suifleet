# Unattended reciprocal PR coordination

The [automatic recovery watchdog](RECOVERY_WATCHDOG.md) adds a five-minute offline progress check and a singleton, bounded recovery-agent session when the pipeline stalls. It complements the dispatcher and PR coordinator while preserving their ownership, review, CI and deployment gates.

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

## Bounded CI failure repair (SF-111)

A ready, same-repository PR with published Review ownership can queue its existing
owner family's repair when a reported merge-required check completes with
`FAILURE` or `TIMED_OUT` and has a durable run/job link. This includes a source
review blocked while CI was pending. Under the existing merge policy every
reported check must succeed. Cancelled, skipped, startup/action-required,
ambiguous rerun ordering, status errors and unavailable run evidence stay merge
blockers without starting source repairs. The newest observation per workflow/job
name supersedes older runs.

CI repair uses a separate reservation fingerprint containing the exact head,
integration base and failed run links/outcomes. It does not change source-review
fingerprints, discard feedback or count as approval. One attempt per fingerprint
is recorded atomically with the normal PR/family reservation in `ci_repairs`.
`ci_check_budget` persists the configured attempt ceiling per failing check
episode, with exponential backoff from five minutes up to one hour. New head/base/run evidence can
receive a new attempt after backoff, but does not reset the episode budget.
Only a fresh snapshot with every check successful (including current-base testnet
when required) clears that budget. Duplicate scans, restarts and pending or
cancelled runs retain attempts and failure history. A reserved fingerprint never
automatically replays, even after a failed launch.

Scan JSON includes `ci_failures` with exact run links and
`ci_repair_blocker` with duplicate/backoff/exhaustion diagnostics, including in
read-only mode. At the limit, inspect the retained job receipts and failure cause
and arrange explicit owner recovery; do not erase reservations to restart workers.
Other PRs continue normally.

Repairs use the existing unchanged claim, branch and token. Before resuming,
the coordinator rereads owner identity and exact live head/base/run evidence,
checks the clean owner worktree, then uses normal resume/assert. A failed ownership
mirror retains the launch intent; recovery republishes and asserts the same claim
without replaying resume. Changed CI base/run evidence during recovery requires
explicit inspection. Existing live/orphaned/quarantined jobs exclude duplicates.
Complete comments, threads and independent source feedback accompany the repair.
All exact-head/base CI, independent review and integration gates remain required.

This change must pass independent review before installation with the normal
installer. Offline tests and read-only live failure inspection do not install or
restart the running coordinator.

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

Manual active or blocked claims without durable worker identity still consume implementation capacity, including expired reservations. Managed claims count once alongside their dispatcher records. Review and repair categories have separate capacity; existing workers are not preempted. A Review claim that the coordinator resumed In progress for a feedback or CI fix job is repair work counted only by fix capacity (SF-118). The exclusion requires exactly one running fix job of the same project and family naming the ticket, whose fenced `repair_claim` token digest, key, branch and worktree match the current published, unexpired owner generation and whose `process.json` matches its live PID/start identity. A snapshot ticket key, reserved/orphaned/finished job, historical schema, ambiguous or stale-generation repair, blocked claim, unreadable state, or an unfinished SF-116 CI redirect or SF-117 capacity recovery of that claim keeps the implementation reservation.

A worker starts only after refresh, fetch, atomic claim, published ownership and assert. It inherits the configured project's account token and claim, heartbeats, pushes scoped user-authored commits, and opens a linked PR. A successful stopped `review_ready` worker with published Review/Done ownership frees its implementation slot. Reconciliation revisits orphaned records after restart, so a delayed valid handoff is not permanently stranded.

Verified stopped blocked workers are **parked**. Verified stopped jobs without a valid handoff are **quarantined**. Both retain ticket ownership, branch, logs and evidence, but no longer consume execution capacity for unrelated tickets. Quarantined review/repair jobs retain exclusion on their original PR. Neither state authorizes automatic claim release, duplicate implementation or retry of an uncertain mutation. The only automatic continuation is the bounded same-claim dependency-wait resume described below. Explicit recovery still requires inspection and the atomic ticket workflow. Implementation slots stop claiming when their combined count of distinct parked/quarantined claims reaches `max_quarantined` (default 3, integer 1–16). Quarantine also applies persistent project/family exponential backoff (60–900 seconds), reset only by a valid completed handoff, never merely by starting a process. Review/repair reservations stop after three quarantined jobs per family/category across projects; each quarantine adds durable exponential backoff (300–3600 seconds). These retention caps require explicit operator recovery; they bound a systemic fast-failure cascade while allowing limited unrelated progress. The diagnostics are visible in dispatcher `--status`, the private review-job database and service journal.

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
It fetches main directly from the configured GitHub repository into a dedicated
`refs/pr-coordinator/activation-main` ref without writing shared `FETCH_HEAD`,
checks installed
ancestry and examines every first-parent change under `tools/pr-coordinator/` since the installed
revision (including validation source that will be executed). Its allowlist is exactly `coordinator.py`, `dispatcher.py`, `worker.py`.
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

### Clean exit without handoff (SF-112)

A quarantined implementation with a durable process receipt, actual exit code
zero and no result file is eligible for one handoff audit. The dispatcher checks
that the wrapper PID is absent (a reused PID fails closed), its session has no
surviving processes, ownership is published and unchanged, and the original
worktree is clean on the original branch. Under the atomic database write lock
it compares the ownership/publication revisions and rechecks stopped identity.
Only then may it renew an expired same-owner lease through the ticket CLI. It
asserts the live claim (including remote-open and dependency gates), reacquires
the authority lock and rechecks ownership and stopped identity before launch.
The source and audit receipts count as one retained claim against the retention
limit; their process identities remain individually checked for active capacity.
Nonzero/unknown exits, malformed results, dirty trees, pending mirrors, changed
claims and uncertain launches remain reserved for explicit recovery.

`handoff_audits` persists a unique source/attempt before launch. The audit uses
the same family, token, session, branch and worktree, with separate logs/process/
exit/result files under the original directory's `handoff-audit/`. It has a
15-minute CLI deadline and cannot audit itself or retry after restart. It must
inspect existing commits, PR/head, real test receipts and current CI; it may
publish a verified Review handoff or record a concrete blocker. It must not
blindly rebuild, create another implementation, manufacture evidence or bypass
review/CI. A timeout does not prove descendants stopped: the normal session
scan continues to hold capacity. Original logs and actual exit receipts remain.

Workers must not detach heartbeat helpers. The dispatcher already renews live
workers. It scans same-user ticket heartbeat command lines and verifies their
claim environment/token, recording matching PID/start identities in
`helpers.json`. Matching helpers prevent audit, retirement and capacity release;
no uncertain PID is signalled. An unreadable candidate helper fails closed.
A stale PID alone never authorizes termination. Operator recovery must verify
and stop such helpers before the audit becomes eligible. As with the existing
session scan this is cooperative process accounting, not containment of an
arbitrary subprocess deliberately escaping its session and hiding its command.
No services are installed/restarted by tests; rollout follows reviewed integration.

### Dependency-wait resume (SF-114)

A worker blocked **only** until prerequisite tickets merge blocks its ticket with
that explanation and writes a structured result:

```json
{"verdict": "blocked", "evidence": "…",
 "dependency_wait": {"tickets": ["SF-083"], "integration": "merged_main"}}
```

The object must contain exactly those two fields, 1–16 distinct other ticket
keys and `merged_main`. Free-form blocked text, errors, nonzero or uncertain
exits, hardware/production/authorization blockers and malformed receipts are not
resumable and stay parked or quarantined for explicit recovery. Prerequisites
named in the acceptance policy's `acceptance_only_tickets` are refused.

Each scan, before new claims, the dispatcher revisits parked receipts with a
valid dependency wait. It requires every prerequisite to be published Done
without a token and its latest `ticket complete` "Merged commit" (a full SHA) to
be an ancestor of freshly fetched `origin/main`. It then checks the clean exit,
stopped wrapper/session and claim helpers, an enabled slot for the original
family, the unchanged published *blocked* claim (token, session, branch,
worktree, agent, model), a clean worktree on the original branch and execution
capacity. The `max_quarantined` retention cap does not prevent a resume, since
resuming releases that retention. Under the atomic database write lock it
compares the ownership/publication revisions and rechecks stopped identity, then
persists a `resuming` intent in `dependency_resumes` before any mutation. The
intent holds its family's execution capacity.

It then runs `ticket resume` (which publishes), `ticket publish` when a mirror
write was left pending, `heartbeat` and `assert` (live claim, open issue,
published mirror and dependencies), rechecks process/worktree/merge state and
records `launched` together with archiving the parked source receipt in one
transaction, before starting the worker. The resumed worker uses the same claim,
session, token, branch and worktree with separate files under the original
directory's `dependency-resume/`; its prompt names the merged prerequisites and
requires merging `origin/main` into the existing branch, resolving conflicts,
continuing unfinished work and rerunning required tests. All review, CI, testnet
and acceptance gates are unchanged; it is a normal worker for reconciliation.

A failure after the intent (mirror, assert or recheck) retries the same intent
with 60–900 second backoff, never a second `ticket resume`, and becomes `failed`
after five attempts. Changed ownership or an unexpected status becomes `failed`
immediately. A failed launch leaves an `orphaned` run and is never relaunched.
A claim may be resumed at most three times; a further wait is `failed`.
Terminal states never restart a worker or release/recover a claim. Duplicate
scans and restarts adopt the durable record; the dispatcher lock prevents
concurrent scanners.

Status: dispatcher `--status` joins each receipt with `dependency_resume`
(`waiting`, `resuming`, `launched`, `failed`), its reason, attempts and
`resumed_as`; the parked receipt message (shown in the dashboard) carries the
current wait reason, for example `waiting for merged prerequisites: SF-083` or
`prerequisites merged; waiting for implementation capacity`. For a `failed`
record, inspect the reason and original/resume receipts. If `ticket resume`
already applied, the ticket is In Progress without a worker; stop any surviving
process, then recover or hand off through the normal atomic ticket workflow. Do
not delete `dependency_resumes` rows to force a relaunch.

### Failed CI on active implementation PRs (SF-116)

The coordinator observes completed current-head/base CI on every owned open
same-repository PR, including drafts and In Progress implementations, not only
Review PRs. Each executing scan stores one `ci_observations` row per failing PR in
`reviews.sqlite3`: ticket, branch, owner status, exact head/base, draft flag,
failed runs, the repair fingerprint and `ci_responsibility`, which names who
actually repairs it (the dispatcher-redirected implementation owner, the
coordinator's Review CI repair, or a manual/stopped/expired owner who needs
explicit recovery). Green, pending, cancelled and superseded snapshots clear the
row; closed PRs are dropped. Scan JSON prints the same `ci_responsibility`.
Review PRs keep the existing coordinator CI repair (SF-111) unchanged.

Each dispatcher scan, before new claims, matches observations to its own live
managed implementation runs (same ticket and branch) and keeps one durable
`ci_redirects` record per claim and failure fingerprint; terminal records never
replay and a claim is redirected at most three times. After a 10-minute owner
grace period from the failed run's completion the dispatcher revalidates against
GitHub (exact head, current base, check runs) every 5 minutes; a fixed, rerun,
cancelled or moved failure is `superseded` without touching the owner. A changed
claim, Review handoff, expired/pending ownership or a stopped/reused worker is
never acted on.

Forced interruption requires the explicit, exhaustive private project setting

```json
"ci_redirect_protected_resources": ["network:suifleet-testnet", "hardware:raspberrypi3bplus"]
```

listing every shared-mutation lock (it must include the testnet authorization
lock). Without it the failure stays visible and the owner is never stopped. The
owner is interrupted only when its session has no other user's process, claim
helper, held lock or Sui CLI/wallet process. The dispatcher then takes real
exclusive `ticket lock` guard leases on every protected resource under a
distinct `ci-redirect-guard-*` session (lock-obeying workers therefore cannot
start a wallet or hardware mutation, and acquiring them proves none is running),
rechecks everything under the fence, persists the exact PID/start identities and
only then sends SIGTERM. SIGKILL follows after 60 seconds, only while every guard
is still asserted and only to identity-verified original session members (a
dead or reused session ID never expands the set). `signalled` and `killed`
record attempts that actually returned; a crash between the persisted intent and
the signal retries the guarded signal on restart instead of quarantining. Zombie
processes count as stopped, while any live original-session descendant keeps the
fence. A worker that has not stopped 5 minutes after SIGTERM (and 30 seconds
after an attempted SIGKILL) becomes `uncertain`: guards are released, the claim
and its capacity stay reserved and the recorded PIDs need explicit inspection.
Guards are released as soon as the stop is verified, before any repair.

The paused feature worktree is never edited until integration. Its complete
working state (tracked, staged and untracked, not ignored) is preserved as user-
authored backup refs `refs/pr-coordinator/ci-redirect/<session>-<id>/{worktree,index}`.
A same-claim repair worker (same family, token, session and branch) runs in a
clean linked checkout detached at the failed head. Its prompt grants no
machine-session shared-network or hardware mutation authority: read-only checks
and isolated local chains only, with public-testnet acceptance through the
normal CI its push triggers; a genuine need for a machine-session testnet or
hardware run is reported as `blocked`. The repair wrapper has no kill timer
(`limits.json` is never written). After 2 hours the dispatcher stops an overdue
repair only through the same guard-fenced SIGTERM/SIGKILL escalation
(`repair_stopping`); a held lock defers it. A `fixed` result must be a
fast-forward push of user-authored commits; `rerun_requested` needs concrete
transient-infrastructure evidence and pushes nothing. The repair is merged into
the paused worktree (fast-forward, or a user-authored merge commit), the
unfinished changes are verified unchanged against the snapshot, and the original
implementation resumes automatically under the same claim with a prompt naming
the repair. A conflict aborts the merge, keeps the work in place and in the
backup refs, and records `conflict` with the manual next step. Blocked or
invalid repairs (`repair_failed`), launch failures (`orphaned`, never
relaunched) and ownership changes keep the claim reserved; verified-stopped
cases are quarantined so they release execution capacity. Failed steps retry
with 60–900 second backoff and become `failed` after eight attempts.

The paused owner and its repair use the owner's single implementation slot; no
review/repair slot is consumed and no second editor runs. `--status` adds
`ci_redirect` and `ci_redirect_reason`. `fleet-status` shows `ci-repair-requested`
(pending, only while the coordinator still observes that exact failure),
`pausing` (guarded interruption), the live repair as a `ci-repair` worker with
family, model, ticket, PR and failed checks plus the implementation as `paused`,
`uncertain`/`orphaned`/quarantined outcomes, and the resumed implementation as
one ordinary worker. Other observed failures appear as `ci-failed` with their
actual repair responsibility. Tests use offline Git repositories, real worker
and fake model processes and a fake lock engine with the real lease semantics;
no tests install or restart services.


### Provider model-capacity recovery (SF-117)

The dispatcher automatically retries a managed implementation only after its CLI
has durably exited nonzero with an explicit terminal provider model-capacity error.
It recognizes the anchored Codex `ERROR: Selected model is at capacity. Please try
a different model.` line (including the known terminal rollout-recording diagnostic)
and matching CLI terminal error envelopes. It reads at most 32 KiB from the output
tail. Usage/authentication failures, signals/timeouts, malformed receipts, user
handoffs, quoted assistant/tool content and unknown trailing errors do not qualify.
Those exits retain the existing quarantine behavior; models are never switched.

Recovery preserves the same atomic claim, configured family/model/command,
branch/worktree, staged changes, unstaged changes and untracked files. It performs
no reset, clean, checkout or automatic merge. The original receipts remain intact.
Before replacement, it verifies the original PID/start identity and entire session
are stopped, finds detached same-claim processes, asserts the unchanged published
In Progress claim, rejects owner-held resource leases and unfinished git operations,
and yields to existing CI redirects/dependency resumes/review repairs. It never
stops or signals a process. Unavailable or unreadable verification defers recovery;
Review/Done/Blocked or changed ownership cancels the pending recovery.

`capacity_retries` in the private dispatch database persists the source receipt,
claim-token digest, attempts, deadline and launch state. Three replacements per
claim use 60/120/240-second exponential delays, shared across all replacement run
names and daemon restarts. Retained valid claims are heartbeated during waits.
Configured implementation capacity still applies; due recoveries precede successor
claims and bypass their idle/quarantine backoff. A persistent 60-second family/project
stagger prevents simultaneous capacity failures from launching a replacement storm.
An atomic launch intent reserves a single replacement before spawning. An uncertain
or interrupted launch is never replayed automatically. Existing pre-upgrade
quarantined/operator-held runs are not retroactively unquarantined; the mechanism
applies to newly reconciled managed exits. Exhaustion retains the claim
for explicit recovery and does not reset the budget.

`fleet-status` displays capacity backoff, attempt/deadline, refusal reason and
exhaustion, without reading output logs or displaying tokens. A stopped backoff
receipt is a retained claim, not a running worker; the actual replacement counts
once against implementation capacity. Runtime installation follows reviewed merge
and the normal activation mechanism. Validation uses offline classifier/fence tests,
real fake-CLI exits, surviving descendants, repeated replacement exhaustion and
read-only matching against the preserved SF-082 incident output; no provider outage
or shared-wallet/hardware mutation is induced for this test.

### Provider quota-reset recovery and CI handoff (SF-145)

Claude workers can stop on an explicit session/weekly quota limit that names its
reset time. The shared classifier (`coordinator.quota_reset`) accepts only a final
terminal line, or a `result` error envelope, that is exactly the observed CLI form
`You've hit your session limit · resets 4:30pm (America/Los_Angeles)` or a weekly
form that names the day (`resets Oct 27, 9am (Zone)`). The receipt rules match
SF-117: a nonzero, non-signal, non-timeout exit receipt, no result receipt, and at
most 32 KiB of output tail. The message carries no date, so the reset is anchored
to the exit receipt's modification time and must be a valid IANA zone. A session
reset must fall within six hours after the exit and a dated reset within eight days.
Five minutes of clock skew before the exit is tolerated. Quoted or prefixed copies,
assistant JSON, authentication/billing/usage text without a time, weekly limits
without a day, unknown zones and trailing errors are rejected. Those exits keep
their existing quarantine. A reset is never guessed. Codex usage-limit text is not
recognized yet and remains quarantined.

**Implementation workers.** A validated quota exit becomes a `capacity_retries` row
marked `quota` in the side table `capacity_kinds`. The SF-117 table layout is unchanged,
so an activation rollback still works; unmarked rows are capacity retries. It uses the same SF-117 fences, launch intent, single-replacement
rule and no-replay guarantees, with a separate budget of four recoveries per claim
token digest. The replacement is due 120 seconds after the stated reset. A reset
that already passed is due immediately, for example after dispatcher downtime. The
claim is asserted and heartbeated while it waits. The quota is shared by the
account, so the project/family `backoff` row also pauses new claims for that family
until the reset (`provider quota reset wait`). Replacement sessions/directories are
`SOURCE-quota-N`. Exhaustion quarantines the run with the claim retained for
explicit recovery.

**Repair workers.** A coordinator fix job with a fenced `repair_claim` that stops
on a validated quota exit becomes `quota_wait`. It keeps the PR reserved against
new review/fix jobs. `quota_recoveries` in the review database records the source
receipt, reset time and state. Before the reset, the coordinator only asserts and
heartbeats the unchanged claim and makes no GitHub reads. Once the reset passes,
it rechecks the following:

- the same published In Progress token digest, key, branch, worktree, family and model;
- an unchanged worker command;
- a stopped original PID/start identity and session;
- no live process carrying the claim token. A same-user process whose environment
  cannot be read (for example a nondumpable helper) counts as carrying it, so the
  wait continues. This includes unrelated nondumpable processes such as `ssh` or
  `gpg-agent` started after the worker. The wait reason names each PID and command
  so an operator can stop it or recover explicitly. The dispatcher's
  implementation fence applies the same rule;
- no live lock naming the claim's session or ticket;
- no unfinished git operation;
- an open same-repository PR on the branch;
- available configured fix capacity.

It then records a launch intent and starts one same-family worker in `quota-N/`
under the same job and claim. Dirty, staged, untracked and already-pushed work is
preserved for the worker to continue. A changed owner or status cancels the wait and
quarantines the job. An interrupted or uncertain launch becomes `orphaned`, with its
process identity cleared, so it is held for explicit recovery and never replayed or
auto-quarantined. Each repair job gets four quota recoveries; exhaustion follows the
existing orphaned → quarantined path. A review job that stops on a quota limit
retries no earlier than the reset. The dispatcher treats a `quota_wait` or
`quota_launch` repair as owning the claim's recovery. `fleet-status` shows
`quota-wait` for both kinds with the attempt and due time.

**CI handoff.** Implementation and repair prompts no longer ask models to wait for
hosted CI. Workers finish meaningful local work and push. Implementation workers
then mark the PR Ready and the ticket Review; repair workers post their feedback
reply. Each records that hosted CI is pending and exits, with no sleep/watch loops,
monitors or detached watchers. Pending CI is never approval. The coordinator still
requires exact-head/current-base checks, fresh independent review and its merge
gates, and dispatches the bounded SF-111/SF-116 CI repair if a check fails.
Reviewers still perform source-only review while CI is unready.

Validation is offline: `tests/test_quota_recovery.py` covers the observed message,
past and future resets, rejected text, fences for changed ownership, live
descendants and shared locks, budgets, restart/no-replay and the CI-handoff prompts.
It reruns the SF-117 fence suite against quota receipts. Runtime restart and later
advancing jobs are verified only after reviewed merged activation.

## Status evidence freshness (SF-147)

`fleet-status --project suifleet --refresh-github --once` refreshes both the
complete issue/PR inventory and current PR checks, including the testnet status.
Check requests are batched (25 PRs per GraphQL request, at most 1,000 PRs and
100 contexts per PR); partial/error/truncated responses preserve the old cache
and report refresh failure rather than claiming a fresh complete result.

The offline display prefers matching GitHub evidence over older worker-start
snapshots. It shows `ci-failed`, `ci-pending`, `awaiting-review`, `ci-unknown`
(missing/mismatched testnet or revision evidence), or `ci-stale` after 15 minutes.
Every result carries its evidence source and age. Testnet success for an old base
is not current success. Ordinary live refresh makes no network calls; use the
explicit refresh when current remote checks matter. This display never grants
merge approval or changes ticket ownership.

The source ages at the top are database/file modification ages, **not service
heartbeats**. A coordinator can scan without changing job records. Historical
retry notices for verified merged PRs are hidden without editing the review DB.
The dashboard-only installer replaces only the CLI and its launchers, leaving
running workers, services and their configuration untouched.

### Testnet failures retain repair ownership

Automatic testnet reports `testnet/package-upgrade` as a commit status, separately
from ordinary CI check runs. The coordinator recognizes a failed status only for
the exact PR head and current base, with a durable GitHub Actions run URL. It
reserves an owner repair through the same deduplication, backoff and configured
failure-episode limit as other CI failures. Infrastructure, funding and uncertain
transaction classifications retain that owner; they are not silently excluded
from dispatch. Repairs must diagnose the linked run, preserve uncertainty and
spending limits, and coordinate shared harness fixes rather than blindly replaying
transactions or increasing retry counts indefinitely.

Pending, cancelled, malformed, unlinked and stale-base statuses do not start a
repair. They still cannot satisfy the merge gate. The dispatcher re-reads the
exact-head testnet status before redirecting an existing implementation. Successful
checks invalidate an earlier source-only review and request a final evidence
review; failure never becomes approval. A repair-limit or ownership blocker stays
visible with its responsible owner and requires a concrete recovery decision.

### Completed repairs and independent check budgets

A successful worker exit with a validated repair result is collected separately
from the ticket handoff. If the worker omitted its handoff, the coordinator checks
the durable exit/process identity, unchanged claim and model, stopped helpers,
shared-resource leases, clean owner branch and current PR before returning the
same reservation to Review. It may renew a verified stopped expired reservation
only to finish this terminal handoff; it neither edits files nor starts a worker.
A blocked verdict remains blocked evidence and never becomes approval. Dirty work,
uncertain receipts or a changed owner stop the handoff and preserve the work.

A failed ticket mirror is retried independently, including after service restart.
The coordinator re-reads ownership so a locally successful Review transition is
published without replaying the transition or worker. Normal exact-head testnet,
source review and merge gates still apply.

Repair limits and backoff apply separately to each workflow/check on a PR, across
head, base and run changes. Three compiler failures cannot consume the first
public-testnet repair. A combined repair counts against each failing check it
handles; a successful full CI snapshot ends those failure episodes. Migration
attributes historical attempts using verified saved snapshots and their existing
repair fingerprints. Missing or corrupt history retains a conservative wildcard
limit, rather than silently granting more retries. The legacy PR-wide counter is
retained as history but no longer blocks unrelated checks.

### Development blockers and merge prerequisites

`depends_on` and native GitHub blocked-by edges stop development claims and
ownership assertions. The coordinator checks those dependencies before reserving
or resuming a repair, so a known blocker does not create a workerless In progress
claim. A pending recovery reports the dependency explicitly and retains its intent.

Use `merge_depends_on` in the ticket specification when the user authorizes
implementation against a pinned, reviewed interface before its upstream PR merges.
It does not block implementation assertions. The coordinator verifies every such
ticket is Done and its ownership publication is current before integration; it
never turns pending upstream code into merge approval. SF-140 requires SF-139 at
merge, and SF-141 requires SF-129 at merge. Keep these requirements in the issues
and catalog rather than also adding native development-blocking edges.

An exact same-claim pre-spawn repair intent can supersede the stopped original
implementation reservation. The dispatcher verifies its claim/branch/worktree,
head, empty process receipts and un-crossed launch boundary. This is retained
repair ownership, not proof of a running worker. An uncertain spawn without a
process receipt is still retained for explicit recovery. Extra CI metadata does
not invalidate otherwise verified core ownership proof for a live repair.


### CI repair attempt ceiling

The top-level private configuration key `ci_repair_attempt_limit` sets the maximum
repair attempts per failing workflow/check, across head, base and run changes.
It accepts an integer from 1 to 100 and defaults to 3. This installation uses 8
under the user's explicit authorization. Restart the coordinator after changing
it. Raising the ceiling preserves existing attempt counts, exponential backoff,
identical-run deduplication, ownership and all review/merge gates. It does not
increase any testnet spending allowance or automatically replay uncertain
transactions. A fully successful CI snapshot ends the failure episode as before.


### Expired-fixture acceptance readiness (SF-156)

`acceptance_watch.py` is a standalone one-shot operator helper for the existing
SF-129/PR279 Testnet fixture. Its user timer checks every 15 minutes while WSL is
running. It reads the existing owner and the read-only `reclaim_plan.cjs` output;
epoch 563 alone is insufficient: the intended device/writer must have an eligible
expired anchor. Before that condition, no model worker, wallet lock or transaction
is started. Node probes time out after 60 seconds.

Once ready, it posts a uniquely marked readiness observation and requests a fix
through the installed coordinator's normal transactional per-PR reservation and
ownership/clean-tree/head checks. The existing Claude claim and branch are kept.
It does not run another coordinator scan or merger. Existing active/reserved or
uncertain jobs prevent a second dispatch; normal coordinator collection handles
its persisted worker/launch receipts. The owner must independently verify chain,
account, authority, bytecode/origins and expiry before the bounded effects-checked
reclaim run. Readiness is not successful acceptance or approval.

The private `~/.local/state/pr-coordinator/sf129-expiry-watch.json` journal records
waiting/dispatch state without claim tokens. A dispatched reservation is one-shot
across timer/service restarts. Supplied reclaim evidence stops further dispatch,
but still needs independent review. An interrupted launch with no matching durable
reservation, or a failed/uncertain reservation, reports `operator_required` and
never replays automatically. Inspect the exact claim/process/job evidence before
any explicit recovery; do not delete the journal just to force another worker.
Probe failures fail closed and are retried on the next timer invocation. Errors
record their type, without exception argv that might contain a claim token.

Install **tested committed source** under the user's maintenance authorization:

```bash
python3 -m unittest discover -s tools/pr-coordinator/tests -p test_acceptance_watch.py -v
# Run before enabling execution: only a read-only readiness probe.
python3 tools/pr-coordinator/acceptance_watch.py \
  --config ~/.config/pr-coordinator/config.json \
  --state ~/.local/state/pr-coordinator \
  --runtime ~/.local/lib/pr-coordinator/current
install -m 0700 tools/pr-coordinator/acceptance_watch.py ~/.local/lib/pr-coordinator/acceptance_watch.py
install -m 0644 tools/pr-coordinator/acceptance-watch.service tools/pr-coordinator/acceptance-watch.timer ~/.config/systemd/user/
# Preserve a working Node/tool PATH; the system Node may be too old for the SDK.
python3 -c 'import os,shutil; print("PATH="+os.path.dirname(shutil.which("node"))+":"+os.path.expanduser("~/.local/bin")+":"+os.path.expanduser("~/.cargo/bin")+":"+os.defpath)' > ~/.config/pr-coordinator/acceptance-watch.env
systemctl --user daemon-reload
systemctl --user enable --now acceptance-watch.timer
systemctl --user start acceptance-watch.service
systemctl --user status acceptance-watch.timer
journalctl --user -u acceptance-watch.service -n 10 --no-pager
cat ~/.local/state/pr-coordinator/sf129-expiry-watch.json
```

The service uses `KillMode=process`, preserving any properly launched worker when
the one-shot helper exits. Its main execution timeout is four minutes; an uncertain
launch remains journaled rather than retried blindly. Disabling the timer stops
future probes, not a worker already running. This fixture-specific helper does not
provision production keys, authorize Mainnet/hardware work, change CI allowances,
mark tickets Done, reset repair budgets or relax independent merge gates. The
standalone helper is installed separately from the coordinator generation; record
its exact commit/hash in the local installation receipt when updating it.


### Ordinary CI runtime recovery (SF-179)

The complete serial `CI / check` job has a 120-minute ceiling. Run 40542334953
(job 122324581852) reached the former 60-minute limit on 2026-10-27; this scoped
ceiling correction preserves every security, simulator, platform and integration
step and the existing caches. It is headroom, not a measured completion time.
All other job limits and merge gates remain unchanged.

After this workflow correction merges, the orchestrator/operator must identify
still-current affected PR heads and update their branches with the reviewed main
revision through the normal integration path before rerunning CI. Rerunning an
old workflow revision alone retains its old ceiling. Verify the open PR head and
base, inspect `gh run view RUN --json headSha,workflowName,event,conclusion`, and
only then use `gh run rerun RUN` for a run whose revision contains the corrected
workflow. If integration already started a fresh CI run, use that run instead.
Do not create empty commits, fabricate statuses or bypass checks. Hosted reruns
and their results remain post-integration work; implementation workers hand off
without watching CI.

Cancelled Actions jobs qualify for bounded owner repair only when fresh read-only
API evidence identifies the same repository, PR head, completed workflow attempt,
job ID and GitHub Actions check run, with the exact failure annotation
"The job has exceeded the maximum execution time of …". The coordinator and
dispatcher share this classifier. Evidence records only IDs/revisions and the
fixed classification `job_timeout`; annotation text and API errors are not saved
or printed. The repair snapshot presents the conclusion as `TIMED_OUT`, while
the underlying cancelled check remains a failed merge gate. Fleet-status therefore
continues to show CI failure and the coordinator names the existing repair owner.

The latest attempt must still be current after reading annotations. Superseded or
ambiguously ordered rollups, queued reruns, changed head/base, manual/concurrency
cancellation, foreign check apps and unavailable/malformed/truncated evidence do
not start repair. Responses above 100 jobs or annotations conservatively remain
unclassified. No elapsed-time heuristic or log-text scraping grants repair.
Existing per-check attempt budgets, ownership, live-worker fences, exact-head/base
CI and independent review still apply. Installation is only through the reviewed
merged-runtime path; this change does not restart services from a worktree.

### Shared discovery snapshots

The coordinator uses the installed atomic-ticketing engine's `ticket_snapshot.py`
provider when available. Install atomic-ticketing 0.3.2 or later before deploying
this coordinator change. The provider is resolved from the `ticket` command's
versioned installation; cache data is shared with dispatcher-triggered CLI
refreshes in `${XDG_STATE_HOME:-~/.local/state}/github-snapshots-v1`.

Only discovery lists are cached, with an absolute 60-second lifetime and
credential/host isolation. Writes invalidate snapshots, and concurrent readers
coalesce. PR details, review feedback, checks, ownership verification and branch
tips remain live: the cache never supplies merge approval. Quota observations are
shared for 60 seconds; discovery reserves 250 GraphQL points and 100 REST
requests for fresh decisions and writes. Server rate limits establish a shared
cooldown rather than a retry loop per worker. Expired/error snapshots are not
served as fallback. Existing exact-head/base and review gates remain in force.

An older ticket engine keeps the previous live transport until upgraded. Updating
source on main does not bypass the existing runtime activation/validation policy.

### Authenticated preparation errors (SF-205)

A terminal `ERROR` status can represent preparation failure or cancellation.
Only preparation failure can enter owner repair, and only after fresh API
evidence authenticates the main-branch `testnet.yml` workflow_run, completed
attempt, successful plan and status publisher, and failed prepare job. The
trusted status publisher emits a small `SUIFLEET_PREPARATION_V1` receipt in its
job log binding the API-created status ID, candidate head, current base,
controller revision, run and attempt. The coordinator and dispatcher read that
specific authenticated job log; they retain only the parsed IDs/revisions.
They recheck the latest attempt/status and current PR head/base after the read.
A forged status URL alone cannot supply this receipt. Missing, expired,
ambiguous or unavailable evidence, cancellation and historical runs without
receipts remain unclassified blockers and never launch preparation repair.

Verified failures in `Checkout authenticated test revision` in SuiFleet route
to the existing **SF-204 / PR #406** incident owner while PR #406 is open and
unmerged. After it closes, checkout failures use ordinary bounded owner repair;
unavailable incident state fails closed. Each affected PR retains a
durable observation naming that owner, while the coordinator excludes this shared
failure from per-PR repair dispatch/budgets and the dispatcher does not interrupt
product workers for it. Mixed observations still route their local failures and
report both repair responsibilities. SF-204's existing implementation/review reservation is
preserved; this route is an incident assignment, not proof the defect is fixed.
Other verified preparation failures use the existing bounded owner-repair path.
Source feedback and base integration still follow their ordinary review paths.

Installation remains merged-only. Both the main-sourced receipt producer and
reviewed coordinator must be active before new receipts can be recognized.
SF-204 controller activation, the historical stalled runs and actual subsequent
exact-head/current-base hosted validation remain unresolved until the operator
records real evidence. This implementation neither backfills receipts nor posts
success, reruns transactions, resets budgets or waives CI, testnet, security or
independent review. There is no Testnet spend for offline routing validation.
