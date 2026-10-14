# Atomic ticketing across repositories on one WSL host

This is a standalone Python 3 CLI with no package/network dependencies beyond authenticated `gh` and Git for live operations. The engine is committed here, installed once as `~/.local/bin/ticket`, and selected by committed `.atomic-ticketing.json` in each repository. It does not import SuiFleet app/tracker code. Both model families use the same CLI and database.

## Installation and registration

```bash
python3 tools/atomic-ticketing/install.py
ticket --version
ticket --root /home/tyler/dev/codex/src/suifleet register
ticket --root /home/tyler/dev/codex/src/vortech register
ticket projects
```

The installer keeps versioned, SHA-256-identified engine copies under `~/.local/share/atomic-ticketing/` and atomically updates its launcher symlink. It refuses to overwrite an unrelated command. Add `~/.local/bin` to PATH if needed. Re-run the installer after a committed engine upgrade. Installed engine copies are local; the source, configs, tests and instructions are version-controlled.

The shared runtime database is `${XDG_STATE_HOME:-~/.local/state}/atomic-ticketing/coordination.sqlite3`. Both workers must inherit the same XDG_STATE_HOME; do not use per-project database overrides, separate WSL instances or Windows/network storage. `--database` exists for isolated tests/explicit deployments, not worker separation. Register each canonical Git repository once. A second clone with the same project ID/repository is rejected; linked worktrees retain the registered Git common directory.

Registration needs no API access and validates origin against the configured SSH URL without modifying it. Each project has its own namespace and freshness marker. Identical ticket keys and relative paths in different repositories are independent; resources such as `hardware:cora-z7`, `hardware:raspberrypi3bplus` or `port:3000` are global reservations. Database claims use one SQLite write transaction for ownership, dependency and path/resource checks.

## Configuration

Committed `.atomic-ticketing.json` declares `schema_version: 1`, a stable `project_id`, GitHub `repository`, exact `git_remote`, GitHub API `account`, Project `owner`/`project_number` (or exact title for discovery), catalog and resource rules. Never store tokens, SSH private keys or local absolute checkout paths in it. Local registration stores canonical paths.

SuiFleet uses `catalog.kind: files` with its committed backlog/publication files. Vortech uses `catalog.kind: github` to read existing issues from its own Project, using `GH-N` keys for issue N. Other repositories can select either adapter. The GitHub adapter records `.` as an issue's paths when there is no explicit `ticket_overrides` scope. Paths are descriptive; overrides can add paths/resources/dependencies.

`resource_rules` attach resources when an issue's paths overlap a configured rule. Broad paths conservatively match hardware/build rules. Ad hoc resources can also be reserved with claim/expand `--resource NAME`, then checked with assert `--resource NAME`. Shared resources stay reserved during review/blocked/expired work. They are the only exclusive scope: file paths never block a claim and need no expansion. These reservations coordinate trusted models; they do not physically lock devices or stop noncompliant processes.

Local or cross-project dependencies can be declared as `KEY` or `project_id::KEY`. A dependency must be locally Done with its GitHub ownership mirror published. Native GitHub blocked-by edges are also read, including open blockers outside the selected catalog. An outside-catalog open GitHub blocker stays blocked until it is closed; declaring a central cross-project dependency does not close its issue automatically.

## Credentials and Project setup

Git SSH and GitHub API authentication are independent. SuiFleet retains the `suifleet` SSH alias and uses API account `tyler-suifleet`; Vortech retains `personal` and uses API account `therealtime`. `gh auth token --user ACCOUNT` is captured privately and passed as GH_TOKEN only to that project's subprocesses. The tool never runs `gh auth switch`, prints tokens or edits SSH configuration. For older single-account gh credential files, the tool verifies the captured token's actual login before any repository operation. It accepts that token only when it matches the configured account. Missing or mismatched credentials fail closed rather than using a different account. Commit author/committer identity remains the user's identity, not the API/model account.

Connect each account through GitHub CLI with repo/project permissions. For an existing Project set its owner/number explicitly. Codex can prepare the configured Project under the user's setup authorization:

```bash
ticket --project vortech setup-project
# Only if no configured matching Project exists:
ticket --project vortech setup-project --create-if-missing
```

Setup creates/links a private Project and missing model fields, initializes only missing ownership/status values and preserves populated fields. An existing incompatible workflow is rejected rather than overwritten. The tool never creates issues. Only Codex may create new tickets unless explicitly excepted by the user. Publishing existing claim fields grants no issue-creation authority.

## SuiFleet legacy migration

```bash
ticket --project suifleet migrate
ticket --project suifleet refresh
```

Migration uses SQLite backup, retains tokens, leases, scopes, branches, worktrees, evidence and completion state, and records backup locations for original events. It locks the old database during the copy and marks it retired so the schema-1 CLI cannot create competing reservations. A crash before retirement leaves central claims blocked by the retirement check; rerun migration. Do not manually delete the old database or backups. No active claim is silently released. Stop long-running legacy workers before migration; retiring a database cannot kill an old process or physically fence its filesystem writes.

## Worker loop

```bash
ticket --project suifleet refresh
ticket --project suifleet claim --agent Codex --model ACTUAL_MODEL --session UNIQUE_SESSION
ticket --project vortech refresh
ticket --project vortech claim --agent Claude --model ACTUAL_MODEL --session DIFFERENT_SESSION
ticket --project suifleet list --all
```

Each refresh reads the live Project/issues/dependencies. Snapshots older than five minutes prevent new claims. The claim chooses priority then key after dependencies, ownership and shared-resource conflicts are checked. It creates an isolated branch/worktree from the selected repository and retains a reservation if worktree or API setup fails. It does not copy uncommitted work or fetch/switch SSH identities. Use a reviewed base ref; integrate prerequisite uncommitted work before a dependent task. Read both canonical context and worktree instructions if they differ.

Save the returned ticket key/token locally. Heartbeat at least every five minutes (fifteen-minute lease), assert after long tools and before committing, and `expand --resource` before using an additional shared resource. Read `list --all`/`show`; record interface decisions, test plans and handoffs with `note`. Review keeps the claim and is not dependency completion. A session can hold one active/blocked implementation across all projects, plus independent review handoffs.

Existing lifecycle commands remain: `heartbeat`, `assert`, `expand`, `note`, `show`, `review`, `block`, `resume`, `release`, `recover`, `complete`, `publish`. Use `ticket --project NAME COMMAND --help` for flags. Completion requires review/evidence and a commit verified as merged into the selected integration ref. Integrate serially; the coordinator does not merge, push, close issues, deploy or grant those permissions.

Expired claims remain reserved. An operator must stop the old worker, preserve its branch/worktree/handoff and confirm recovery with the expected token. Invalid tokens require immediately stopping edits. API failures retain local reservations/pending revisions; retry `publish` before editing. Tokens fence coordinator mutations, not operating-system writes. Reopening/reworking completed work needs integrator coordination rather than deleting/editing the database.

## Tests

`make test-coordinator` runs the existing lifecycle/race regression suite plus generic multi-project tests. The portable generic suite is also runnable with `python3 -m unittest discover -s tools/atomic-ticketing/tests`. It uses temporary databases/Git fixtures and fake APIs; no live issues/Projects are changed by tests. Coverage includes cross-project duplicate keys, independent file scopes, global resource races, credential isolation, registration identity, native/central dependencies, mirror failures and legacy state preservation.
