# Issue tracking and model ownership

Repository: `tyler-suifleet/suifleet`. Live private GitHub Project: [SuiFleet — OTA and Fleet Management](https://github.com/users/tyler-suifleet/projects/1). The expanded roadmap has 39 scoped items; see publication records for live issue IDs. See [the issue index](tracking/INDEX.md) and [publication records](tracking/github-state.json); live GitHub state is authoritative for assignments and status.

## Authority

**Only Codex may create new tickets/issues unless the user explicitly authorizes another model.** This covers sub-issues and Project draft issues as well. Humans remain in control. Other models may implement/review assigned existing issues and report findings to Codex through those issues/handoffs. Assignment is not permission to create tickets. See [AGENTS.md](../AGENTS.md) and [CLAUDE.md](../CLAUDE.md).

GitHub authenticates an account, not an AI model. These files establish the requested workflow rule; they do not make shared credentials technically model-restricted. Do not claim that an issue template or a self-reported actor flag enforces access control.

## Backlog and publication

[backlog.json](tracking/backlog.json) contains 39 scoped, dependency-linked roadmap items from the parity audit and upgrade discussion. Each has evidence, affected paths, acceptance criteria and specific verification expectations. Stable keys `SF-001` etc. are local tracking keys, not GitHub issue numbers.

```bash
# Offline preview; no GitHub access or writes required
make tracking-plan
make test-tracking

# Codex only unless the user explicitly grants another model an exception:
python3 scripts/bootstrap_tracking.py --apply
```

The bootstrap uses authenticated `gh`, enables repository Issues if necessary, reuses or creates a private owner-level Project, links the repository, creates missing labels/fields and issues, and adds them to the Project. It discovers existing issues by a stable body marker/title and preserves their contents; retries reuse created records. It does not reset existing assignments/status or close existing work. New seed items start Backlog and Unassigned. Dependencies become issue links and native GitHub blocked-by relationships; existing unrelated dependency edges are preserved.

`docs/tracking/github-state.json` is written only on completed publication and records actual Project/issue URLs. Inspect live GitHub state if publication fails partway; rerun after addressing access/errors. The script is an aid for Codex, not an authorization bypass for other models. Plan/tests never mutate GitHub.

## Connect GitHub

Offline preview/tests require Python 3 on the host.

Either install/connect the GitHub integration in the agent application and authorize this repository, or install a current GitHub CLI in the same Linux/WSL environment as the checkout. Use the [official Linux installation instructions](https://github.com/cli/cli/blob/trunk/docs/install_linux.md), then:

```bash
gh auth login --hostname github.com --web
gh auth refresh --hostname github.com -s project
gh auth status
gh repo view tyler-suifleet/suifleet
```

Keep the existing SSH remote; HTTP API authentication does not require changing Git remotes. Repository access must permit Issues writes and the Project owner must allow Projects writes; organization/SSO policies may require additional approval. Do not paste tokens or recovery codes into chat or commit credentials.

The Project API requires the `project` OAuth scope for GitHub CLI authentication. See [GitHub CLI Projects](https://cli.github.com/manual/gh_project). A connector may expose Issues but not Project writes; if so use the authenticated CLI for the Project operations.

## Fields and views

| Field | Meaning |
|---|---|
| Status | Backlog, Ready, In progress, Review, Blocked, Done |
| Priority | P0 release blocker; P1 OTA/fleet requirement; P2 broader compatibility |
| Area | contracts/security/frontend/clients/edge/runtime/fleet/storage/infra/compatibility |
| Phase | baseline, ota, fleet, extended |
| Agent | Unassigned, Codex, Claude, Other |
| Model | Actual implementation model/version; text, not assumed from agent family |
| Review model | Independent reviewer/model or human; text |
| Work branch | Working branch/worktree/PR reference; text |

GitHub assignees are real account owners; `Agent`/`Model` describe who performs the implementation. The bootstrap creates a Workflow board and All work, P0 release blockers, Codex work, Claude work and Unassigned work table views with ownership columns visible. Further grouping/sorting and optional auto-add workflows can be configured in the Project UI. GitHub reserves Reviewer-related field names; the custom `Review model` field tracks model review separately from native PR reviewers.

For a new Project, the bootstrap initializes the six Status values. For an existing Project it preserves Status options and uses Backlog or Todo for newly added items; do not overwrite an active project's workflow casually.

## Priority and sequence

P means priority/criticality, not issue number or a strict implementation order. P0 blocks release, P1 covers core OTA/fleet requirements, and P2 covers broader Greengrass compatibility. P2 features are still required if full Greengrass equivalence is the goal. Respect dependency edges and coordinate shared interfaces; independent work can proceed concurrently. The SF key and GitHub issue number are identifiers, not priorities.

See [capability coverage](GREENGRASS_TICKET_COVERAGE.md) and [the Raspberry Pi acceptance plan](RASPBERRY_PI_TEST_PLAN.md).

## Atomic implementation claims on WSL

Both models use [the shared SQLite coordinator](ATOMIC_TICKETING.md) before implementation. The installed reusable `ticket --project suifleet` CLI uses the central WSL database shared with Vortech, namespaces ticket ownership by repository and supports separate resource locks globally. It atomically filters priority, dependencies and existing ownership and creates one worktree per claim. File paths are descriptive and are not reserved. Project fields mirror reservations; direct field edits are not the implementation claim mechanism. Read the runbook for heartbeat, locks during actual resource use, local notes, review and stopped-worker recovery. The standalone coordinator repository’s `make test` verifies concurrency and failure behavior without GitHub mutations. Hardware labels on tickets do not reserve devices; development and image builds do not require a Pi lock.

## Assignment and handoff

1. The user selects work or either model requests the next eligible existing issue through the coordinator. Its atomic claim assigns one implementation owner and mirrors Agent/Model, Work branch and Status to the Project. Resolve dependencies before claiming; existing human assignees are reserved and require coordinated handoff.
2. Record scope and test plan in the existing issue. Use a separate branch/worktree for concurrent models; agree on shared API/lockfile/manifest interfaces before parallel edits.
3. If blocked, move Blocked and report the exact dependency/evidence. If a new problem is found, report it to Codex without creating another ticket.
4. Open/link a PR for the issue and move Review with changed behavior, exact commands/results, risks/migrations and anything unverified. Ask another model/human to review where appropriate.
5. Close/move Done only after the issue's acceptance criteria and required local/live/hardware evidence are fulfilled. Compilation or simulator success alone does not satisfy a live-upgrade or firmware recovery issue.

The current release blockers are SF-001 (old-package security), SF-002 (per-type deployment manifest), SF-003/004 (supported clients/transports), SF-005 (shared interfaces), SF-006 (publishing), SF-007 (CI), SF-008 (revocation), SF-009 (execution isolation) and SF-010 (live acceptance). Work on non-overlapping SF-001, SF-002, SF-007 and SF-009 can be scoped in parallel; changes to interfaces require coordination before dependent client work.
