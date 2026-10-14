# Atomic ticket coordination on WSL

Codex and Claude use the same coordinator before starting any implementation. GitHub remains the issue/specification tracker; the local SQLite database is authoritative for implementation reservations. Project ownership fields mirror those reservations. This tool never creates issues or starts models.

## Shared database and isolation

The database is `<git-common-dir>/suifleet-coordinator.sqlite3`, shared automatically by the canonical checkout and every linked Git worktree. Transactions use SQLite `BEGIN IMMEDIATE`, WAL and full synchronization; a claim checks ticket ownership, dependency completion and all overlapping path reservations before acquiring ownership in the same transaction. Claims are sorted by P0/P1/P2 then stable SF key, after filtering dependencies and conflicts.

Both models must use this one Git repository on the WSL Linux filesystem. Separate clones have separate databases and must not be used as competing workers. Do not put the database on `/mnt/c`, a network share or a synced directory. A separate worktree is created for each claim, on a unique branch; failed worktree creation leaves the reservation intact for inspection. It does not reset or copy another worker's working changes. Broad paths and wildcard scopes deliberately reserve whole directories; expand scope before editing additional files.

The database stores model/session identifiers, paths, lease state and local handoff events, not account credentials. GitHub authentication is provided by the existing `gh` setup. No dependency installation is required beyond Python 3, SQLite support, Git and authenticated GitHub CLI.

## Initialize and claim

Run from the canonical checkout after these instruction/tool changes have been committed so new worktrees contain them:

```bash
python3 scripts/ticket_coordinator.py init
python3 scripts/ticket_coordinator.py refresh
python3 scripts/ticket_coordinator.py claim \
  --agent Codex --model ACTUAL_MODEL --session UNIQUE_CODEX_SESSION
```

Claude uses the same command with `--agent Claude`, its actual model and a different session identifier. Each independently asks for the next eligible ticket; the transaction prevents duplicate claims and overlapping scopes. `--ticket SF-029` requests a specific ticket but cannot bypass dependencies or reservations. `--path extra/file` adds scope at claim time. Use `--base REF` to select a tested integration baseline; the default is the canonical checkout's current HEAD. `--worktree /absolute/new/path` overrides the default sibling `suifleet-worktrees` location.

Initialization/refresh reads the live Project, all published issue states and native blocked-by dependencies (including dependencies added after seeding). Open blockers outside the catalog prevent claims. Assigned, blocked, closed and already-owned remote items are not claimed. An unclaimed remote ticket with an existing owner also reserves its catalog paths. Refresh before every new claim: snapshots older than five minutes fail closed. An older/incomplete catalog is rejected; run the canonical checkout's script with `--root /absolute/canonical/checkout` when necessary.

The successful command emits one JSON result with `key`, `token`, `worktree`, `branch`, scope and ownership. Enter that worktree, read its AGENTS.md, and record a testing plan. Scope changes from a refresh can expand outstanding reservations but never shrink them. No worker should implement directly in the canonical checkout.

## Work and communicate

Replace TICKET and TOKEN below with the returned values; tokens identify claim generations and stay local.

```bash
python3 /absolute/canonical/checkout/scripts/ticket_coordinator.py heartbeat TICKET --token TOKEN
python3 /absolute/canonical/checkout/scripts/ticket_coordinator.py assert TICKET --token TOKEN --path path/to/edit
python3 /absolute/canonical/checkout/scripts/ticket_coordinator.py expand TICKET --token TOKEN --path additional/path
python3 /absolute/canonical/checkout/scripts/ticket_coordinator.py note TICKET --token TOKEN --text 'Interface decision, test plan or handoff'
python3 /absolute/canonical/checkout/scripts/ticket_coordinator.py list
python3 /absolute/canonical/checkout/scripts/ticket_coordinator.py show TICKET
```

Heartbeat at least every five minutes, including during long builds. The lease lasts fifteen minutes. Check ownership before an edit batch and again after long tools, before staging/committing or any deployment. `assert` rejects expired, replaced, non-implementation or unpublished claims and paths outside scope. A worker with a lost token must stop editing immediately. A still-owning expired worker may heartbeat and resume; expiry alone never transfers ownership.

Read `list` and relevant `show` events before changing shared interfaces. Local notes provide the common mailbox; GitHub issue comments may record durable human-facing handoffs under the task's authorization. Neither model has automatic access to the other's private conversation. Agree on interfaces in notes/issues; dependency declarations and path locks cannot detect every semantic conflict. Shared build outputs, ports, caches and hardware also need coordination; worktrees isolate source files, not those resources.

## Review, blockers and completion

```bash
python3 scripts/ticket_coordinator.py review TICKET --token TOKEN \
  --text 'PR/branch, changed behavior, exact tests/results, limitations and acceptance evidence'
python3 scripts/ticket_coordinator.py block TICKET --token TOKEN --text 'Concrete blocker and handoff'
python3 scripts/ticket_coordinator.py resume TICKET --token TOKEN
```

Review and blocked work retain their path reservations. Review has no expiring lease and is not dependency completion. A session may select another independent ticket after submitting review, but may own only one active/blocked implementation. Resume is rejected if that session already owns another implementation. Coordinate a reviewer in the issue/Project, and integrate changes serially instead of having workers push directly to main.

After review, required acceptance and integration, the integrator records completion:

```bash
python3 scripts/ticket_coordinator.py complete TICKET --expect-token TOKEN \
  --reviewer REVIEWER --merged-commit COMMIT --integration-ref main \
  --text 'Review and required acceptance evidence'
```

The command verifies the commit exists and is an ancestor of the chosen integration ref. It does not assess the truth of supplied evidence, merge, close the GitHub issue, push, deploy or grant those permissions. Completion unblocks dependencies only after the ownership mirror is published. Locally accepted completion is preserved through refresh; closing an issue without Project Done does not automatically satisfy dependency acceptance. Ticket reopening/rework must be coordinated with the integrator rather than silently editing the database.

## Failure and recovery

Claims and mutations mirror Status, Agent, Model and Work branch to the existing GitHub Project. Remote writes occur under the SQLite transaction to serialize mirrors. A network/API failure leaves the durable claim and pending revision intact; never assume a failed command means no claim was acquired. Inspect `show`, then retry:

```bash
python3 scripts/ticket_coordinator.py publish TICKET
```

`--local-only` on claim/state transitions deliberately leaves publication pending; do not start editing until `publish` succeeds. It is for temporary troubleshooting, not a second coordination mode. Field writes are idempotent; partial remote writes retry the latest local revision. The script does not overwrite live issue descriptions, assignees or acceptance checkboxes.

An expired or crashed worker is never automatically reclaimed. First stop that worker and inspect/save its branch/worktree and handoff. Then:

```bash
python3 scripts/ticket_coordinator.py recover TICKET --expect-token OLD_TOKEN \
  --confirm-worker-stopped --text 'Stopped worker; saved work and handoff location'
```

A worker voluntarily giving up work uses `release --token TOKEN --confirm-stopped --text ...`. These commands invalidate the old token but leave the worktree/branch for recovery. The confirmation flags record an operator assertion; they do not kill processes. Refresh before a replacement claim. Do not delete a live database, edit reservations by hand or reclaim because a clock expired.

This is cooperative coordination for trusted models on one user account, not an OS security boundary: an agent ignoring AGENTS.md can still edit files or GitHub directly. Tokens fence coordinator operations; they cannot physically stop an old process from writing. Never promise conflict-free autonomous operation without worker compliance and serialized review/integration.

## Verification

`make test-coordinator` runs offline tests using temporary databases and worktrees, including a ten-process race with exactly one winner, scope overlap, dependency gating, stale leases, token replacement, remote ownership, failed worktree setup and failed mirror retry. `make test-tracking` checks the existing backlog/publication tooling. Tests do not mutate GitHub.
