@AGENTS.md

# Claude-specific instructions

The imported `AGENTS.md` is the shared infrastructure, build, test, deployment and collaboration guide. Read it before acting.

**Do not create new tickets, GitHub issues, sub-issues or draft issues unless the user explicitly authorizes Claude to do so. Codex is the only agent with default ticket-creation authority.** An implementation assignment does not grant ticket-creation permission. Do not run the tracking bootstrap with `--apply` without that explicit exception.

Work on existing assigned issues. Report additional findings to Codex through the existing issue or the handoff, without creating a new tracking item. Record Claude/model version, branch, changed areas, dependencies and exact test results. Keep operational instructions in `AGENTS.md` and its linked runbooks rather than maintaining a divergent copy here.

For “implement the next ticket”, use `ticket --project suifleet` and the shared coordinator from AGENTS.md with `--agent Claude`, your actual model and a unique session. Enter the returned worktree; keep its lease and token valid. Do not independently claim through GitHub fields, use a separate clone, or create issues.

Use [the unattended PR service](docs/PR_COORDINATION.md) for reciprocal Codex/Claude reviews, feedback repair and serial gated integration. When `PR_COORDINATOR_ACTIVE=1`, complete only the assigned worker task and its structured evidence receipt; do not launch recursive workers, merge independently or create issues. An inherited feedback claim must be asserted before edits/commits, and approval requires the current head and complete ticket acceptance.

Automatic implementation dispatch is described in [PR coordination](docs/PR_COORDINATION.md). When enabled, `ticket-dispatcher.service` refreshes and atomically claims the next eligible ticket for an available configured model slot. Workers must finish their assigned implementation, provide evidence, hand off to Review and exit; they must not claim successors themselves. Failed or uncertain workers retain reservations and stop their slot until explicit recovery. Review/merge checks and deployment authorization still apply.

User-authorized implementation/integration acceptance split: follow [IMPLEMENTATION_ACCEPTANCE.md](docs/IMPLEMENTATION_ACCEPTANCE.md) and its per-ticket tracked scope. Reviewed implementations may merge before the explicitly deferred Pi, real-wallet, public-network or production-release integration checks. Keep code findings, local compatibility/regression evidence and CI as merge gates; retain the linked acceptance tickets and never claim deferred tests ran.

The user explicitly authorizes automatic public Sui testnet acceptance using the dedicated testnet agent/CI wallets described in PR #63 and SF-010/#11. Verify chain/account/authority, preserve keys, honor run spend limits and acquire `network:suifleet-testnet` only during machine-session mutations; coordinate with Claude before using shared state. Mainnet, other wallets, physical programming and production credential operations remain outside this grant. Testnet availability does not reinstate blanket external-acceptance merge blockers.

Public testnet is a normal automated test target under the standing user grant, without a human approval per run. Follow [TESTNET.md](docs/TESTNET.md) for main-only trusted CI, dedicated wallets, exact-head effects evidence, strict spending, encrypted device-state caching and bounded automatic faucet funding. Keep other environment approvals and deferred wallet/Pi/public-Walrus acceptance intact.

Continuous-development standing authorization is in AGENTS.md. Do not wait for the
user to request another review, feedback repair or eligible successor. The existing
services dispatch that work; do not spawn recursive workers. For current and future
software tickets, physical-hardware and actual-mainnet acceptance may wait until
development is complete under the shared policy. Preserve those acceptance records,
all local/security/testnet/review gates and the Codex-only ticket-creation rule.

Latest user direction (2026-10-21): finish implementation before final external
acceptance. Production Seal-provider validation and manual real-wallet/IdP/prover
acceptance no longer block implementation merges; preserve them explicitly in
SF-087/#159 and SF-031/#32 under the shared policy. Do not waive actionable code or
security findings, software tests, applicable automated testnet or independent
review. Do not provision production credentials or claim deferred checks passed.
