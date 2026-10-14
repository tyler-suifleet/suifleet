# Atomic ticket coordination on WSL

Codex and Claude use the same coordinator before starting any implementation. GitHub remains the issue/specification tracker; the local SQLite database is authoritative for implementation reservations. Project ownership fields mirror those reservations. This tool never creates issues or starts models.

## Shared database and isolation

The reusable `ticket` CLI uses `~/.local/state/atomic-ticketing/coordination.sqlite3` (or the shared XDG_STATE_HOME location) across SuiFleet and Vortech. Project configuration is committed in `.atomic-ticketing.json`; the engine, installer, tests and cross-project instructions live in [therealtime/atomic-ticketing](https://github.com/therealtime/atomic-ticketing) (formerly `tools/atomic-ticketing/` here). The original Git-common-directory database is migrated and retired, with a SQLite backup and ownership tokens preserved. Transactions use SQLite `BEGIN IMMEDIATE`, WAL and full synchronization; a claim checks ticket ownership and dependency completion before acquiring ownership in the same transaction. Claims are sorted by P0/P1/P2 then stable SF key, after filtering dependencies and ownership.

Both models must use the same central database and each project's registered canonical Git repository/linked worktrees on the WSL Linux filesystem. Registration rejects a separate clone with an already-registered project identity. Do not put the database on `/mnt/c`, a network share or a synced directory. A separate worktree is created for each claim, on a unique branch; failed worktree creation leaves the reservation intact for inspection. It does not reset or copy another worker's working changes. File paths are not reserved: a ticket's paths describe its likely area, never block another claim and need no expansion. Overlapping edits in separate worktrees are resolved at review/merge.

The database stores model/session identifiers, paths, lease state and local handoff events, not account credentials. GitHub authentication is provided by the existing `gh` setup. No dependency installation is required beyond Python 3, SQLite support, Git and authenticated GitHub CLI.

## Initialize and claim

Run from the canonical checkout after these instruction/tool changes have been committed so new worktrees contain them:

```bash
git clone git@personal:therealtime/atomic-ticketing.git ~/dev/claude/src/atomic-ticketing   # once
python3 ~/dev/claude/src/atomic-ticketing/install.py
ticket --root /home/tyler/dev/codex/src/suifleet register
ticket --project suifleet migrate
ticket --project suifleet init
ticket --project suifleet refresh
ticket --project suifleet claim \
  --agent Codex --model ACTUAL_MODEL --session UNIQUE_CODEX_SESSION
```

Claude uses the same command with `--agent Claude`, its actual model and a different session identifier. Each independently asks for the next eligible ticket; the transaction prevents duplicate claims. `--ticket SF-029` requests a specific ticket but cannot bypass dependencies or ownership. `--resource NAME` only adds a descriptive resource label; it does not acquire a lock. Use `--base REF` to select a tested integration baseline; the default is the canonical checkout's current HEAD. `--worktree /absolute/new/path` overrides the default sibling `suifleet-worktrees` location.

Initialization/refresh reads the live Project, all published issue states and native blocked-by dependencies (including dependencies added after seeding). Open blockers outside the catalog prevent claims. Assigned, blocked, closed and already-owned remote items are not claimed. An unclaimed remote ticket with an existing owner retains its ticket ownership. Refresh before every new claim: snapshots older than five minutes fail closed. An older/incomplete catalog is rejected; use the installed `ticket --project suifleet` CLI against the registered canonical checkout when necessary.

The successful command emits one JSON result with `key`, `token`, `worktree`, `branch`, scope and ownership. Enter that worktree, read its AGENTS.md, and record a testing plan. Hardware locks are independent of ticket claims. No worker should implement directly in the canonical checkout.

## Work and communicate

Replace TICKET and TOKEN below with the returned values; tokens identify claim generations and stay local.

```bash
ticket --project suifleet heartbeat TICKET --token TOKEN
ticket --project suifleet assert TICKET --token TOKEN
ticket --project suifleet note TICKET --token TOKEN --text 'Interface decision, test plan or handoff'
ticket --project suifleet list
ticket --project suifleet show TICKET
```

Heartbeat at least every five minutes, including during long builds. The lease lasts fifteen minutes. Check ownership after long tools and before staging/committing or any deployment. `assert` rejects expired, replaced, non-implementation or unpublished claims, and, when `--resource` is supplied, missing live resource locks for the owning session. `--path` is accepted for older instructions and ignored. A worker with a lost token must stop editing immediately. A still-owning expired worker may heartbeat and resume; expiry alone never transfers ownership.

Read `list` and relevant `show` events before changing shared interfaces. Local notes provide the common mailbox; GitHub issue comments may record durable human-facing handoffs under the task's authorization. Neither model has automatic access to the other's private conversation. Agree on shared interfaces in notes/issues; dependency declarations cannot detect every semantic conflict. Configured `resource_rules` and claim/expand `--resource` attach descriptive resource labels. They do not block ticket claims or reserve devices. Worktrees isolate source files, not devices or ports.

Acquire a separate lock immediately before actual shared resource use. Software development, client/image builds and simulator tests do not require a Raspberry Pi lock. Flashing, booting, deploying to or testing the physical Pi does. Locks are global across projects and have their own tokens and TTL; a ticket heartbeat does not renew them.

```bash
ticket --project suifleet lock hardware:raspberrypi3bplus \
  --agent Codex --model ACTUAL_MODEL --session UNIQUE_CODEX_SESSION \
  --purpose "Hardware acceptance" --ticket TICKET
# Use the token returned above, independent of the ticket token.
ticket --project suifleet lock-assert hardware:raspberrypi3bplus --lock-token LOCK_TOKEN
ticket --project suifleet lock-renew hardware:raspberrypi3bplus --lock-token LOCK_TOKEN
ticket --project suifleet unlock hardware:raspberrypi3bplus --lock-token LOCK_TOKEN
ticket locks
```

Assert the lock before device operations, renew before its TTL expires while using the device, and release promptly when finished. If renewal/assertion fails, stop device operations and coordinate recovery. Review or blocked status does not reserve hardware. Use the same separate lock workflow for an actually shared build environment or port; ordinary isolated builds need no lock.

## Review, blockers and completion

```bash
ticket --project suifleet review TICKET --token TOKEN \
  --text 'PR/branch, changed behavior, exact tests/results, limitations and acceptance evidence'
ticket --project suifleet block TICKET --token TOKEN --text 'Concrete blocker and handoff'
ticket --project suifleet resume TICKET --token TOKEN
```

Review and blocked work keep their ticket claim, independently of resource locks. Review has no expiring lease and is not dependency completion. A session may select another independent ticket after submitting review, but may own only one active/blocked implementation across all registered projects. Resume is rejected if that session already owns another implementation. Coordinate a reviewer in the issue/Project, and integrate changes serially instead of having workers push directly to main.

After review, required acceptance and integration, the integrator records completion:

```bash
ticket --project suifleet complete TICKET --expect-token TOKEN \
  --reviewer REVIEWER --merged-commit COMMIT --integration-ref main \
  --text 'Review and required acceptance evidence'
```

The command verifies the commit exists and is an ancestor of the chosen integration ref. It does not assess the truth of supplied evidence, merge, close the GitHub issue, push, deploy or grant those permissions. Completion unblocks dependencies only after the ownership mirror is published. Locally accepted completion is preserved through refresh; closing an issue without Project Done does not automatically satisfy dependency acceptance. Ticket reopening/rework must be coordinated with the integrator rather than silently editing the database.

## Failure and recovery

Claims and mutations mirror Status, Agent, Model and Work branch to the existing GitHub Project. Remote writes occur under the SQLite transaction to serialize mirrors. A network/API failure leaves the durable claim and pending revision intact; never assume a failed command means no claim was acquired. Inspect `show`, then retry:

```bash
ticket --project suifleet publish TICKET
```

`--local-only` on claim/state transitions deliberately leaves publication pending; do not start editing until `publish` succeeds. It is for temporary troubleshooting, not a second coordination mode. Field writes are idempotent; partial remote writes retry the latest local revision. The script does not overwrite live issue descriptions, assignees or acceptance checkboxes.

An expired or crashed worker is never automatically reclaimed. First stop that worker and inspect/save its branch/worktree and handoff. Then:

```bash
ticket --project suifleet recover TICKET --expect-token OLD_TOKEN \
  --confirm-worker-stopped --text 'Stopped worker; saved work and handoff location'
```

A worker voluntarily giving up work uses `release --token TOKEN --confirm-stopped --text ...`. These commands invalidate the old token but leave the worktree/branch for recovery. The confirmation flags record an operator assertion; they do not kill processes. Refresh before a replacement claim. Do not delete a live database, edit reservations by hand or reclaim because a clock expired.

This is cooperative coordination for trusted models on one user account, not an OS security boundary: an agent ignoring AGENTS.md can still edit files or GitHub directly. Tokens fence coordinator operations; they cannot physically stop an old process from writing. Never promise conflict-free autonomous operation without worker compliance and serialized review/integration.

## Verification

The extracted coordinator's `make test` runs offline tests using temporary databases and worktrees, including a ten-process race with exactly one winner, non-blocking path overlap, independent resource-lock conflicts, dependency gating, stale leases, token replacement, remote ownership, failed worktree setup and failed mirror retry. `make test-tracking` checks the existing backlog/publication tooling. Tests do not mutate GitHub.
