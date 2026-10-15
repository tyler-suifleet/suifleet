@AGENTS.md

# Claude-specific instructions

The imported `AGENTS.md` is the shared infrastructure, build, test, deployment and collaboration guide. Read it before acting.

**Do not create new tickets, GitHub issues, sub-issues or draft issues unless the user explicitly authorizes Claude to do so. Codex is the only agent with default ticket-creation authority.** An implementation assignment does not grant ticket-creation permission. Do not run the tracking bootstrap with `--apply` without that explicit exception.

Work on existing assigned issues. Report additional findings to Codex through the existing issue or the handoff, without creating a new tracking item. Record Claude/model version, branch, changed areas, dependencies and exact test results. Keep operational instructions in `AGENTS.md` and its linked runbooks rather than maintaining a divergent copy here.

For “implement the next ticket”, use `ticket --project suifleet` and the shared coordinator from AGENTS.md with `--agent Claude`, your actual model and a unique session. Enter the returned worktree; keep its lease and token valid. Do not independently claim through GitHub fields, use a separate clone, or create issues.

Use [the unattended PR service](docs/PR_COORDINATION.md) for reciprocal Codex/Claude reviews, feedback repair and serial gated integration. When `PR_COORDINATOR_ACTIVE=1`, complete only the assigned worker task and its structured evidence receipt; do not launch recursive workers, merge independently or create issues. An inherited feedback claim must be asserted before edits/commits, and approval requires the current head and complete ticket acceptance.

Automatic implementation dispatch is described in [PR coordination](docs/PR_COORDINATION.md). When enabled, `ticket-dispatcher.service` refreshes and atomically claims the next eligible ticket for an available configured model slot. Workers must finish their assigned implementation, provide evidence, hand off to Review and exit; they must not claim successors themselves. Failed or uncertain workers retain reservations and stop their slot until explicit recovery. Review/merge checks and deployment authorization still apply.
