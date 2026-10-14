@AGENTS.md

# Claude-specific instructions

The imported `AGENTS.md` is the shared infrastructure, build, test, deployment and collaboration guide. Read it before acting.

**Do not create new tickets, GitHub issues, sub-issues or draft issues unless the user explicitly authorizes Claude to do so. Codex is the only agent with default ticket-creation authority.** An implementation assignment does not grant ticket-creation permission. Do not run the tracking bootstrap with `--apply` without that explicit exception.

Work on existing assigned issues. Report additional findings to Codex through the existing issue or the handoff, without creating a new tracking item. Record Claude/model version, branch, owned paths, dependencies and exact test results. Keep operational instructions in `AGENTS.md` and its linked runbooks rather than maintaining a divergent copy here.
