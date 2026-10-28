# Automatic fleet recovery

The dispatcher starts eligible implementation work. The PR coordinator starts reviews and repairs and performs gated merges. The recovery watchdog checks whether that pipeline is making progress and starts a bounded Codex session when it needs investigation. It addresses the gap where both services are running but claims, publication, CI or dependencies have stopped work.

## Detection and recovery

Every five minutes, `recovery-watchdog.timer` invokes the watchdog. It reads `fleet-status --json` offline, so the monitor adds no recurring GitHub queries. Existing reporting and orchestration services remain responsible for their own live reads.

An enabled project becomes a recovery candidate after 15 minutes without meaningful observed progress when there is open backlog, implementation or review work without a verified worker, a waiting repair, uncertain ownership, failed CI, or unavailable inventory. The recovery grace clock resets only when the available local inventory records an increase in completed tickets. Worker identities, starts/exits, nonterminal queue changes, CI-notice churn and timestamp refreshes are activity, not completion; they cannot repeatedly postpone recovery. Ordinary verified active work remains supervised without launching an idle recovery agent. When that work stops without completion, it does not receive another grace period. Inventory loss/return and migration from older saved state preserve the prior deadline. The report distinguishes active workers, observation grace with an exact recovery deadline, cooldown/provider waits, and no actionable work. Active ordinary work and projects containing only blocked external acceptance do not start idle agents.

The recovery prompt tells the agent to inspect the cause, take concrete authorized actions, verify actual processes and tracker state, and report remaining blockers. It can repair operational stalls using the installed ticket and coordination tools. It must preserve live ownership, prove stopped workers before recovering expired claims, respect maintenance and resource locks, and leave merges to the single coordinator. Code changes still require a valid issue claim.

A stopped implementation that still needs code or CI repairs can be resumed even when its ticket is Blocked and its PR is draft. The recovery agent invokes the installed `recovery_control.py --project PROJECT --ticket TICKET --reason 'specific repair instruction'`. This preserves the original family/model, token, branch and worktree. It refuses live or uncertain workers/helpers, pending competing repairs, incomplete dependencies, resource leases, changed commands, unfinished Git operations and exhausted configured capacity. It briefly pauses only the two service parents under their authority locks; `KillMode=process` preserves their existing workers. A durable replacement reservation precedes ticket resumption and launch. The existing dispatcher then supervises the replacement normally. An uncertain launch remains held; it is never replayed automatically. Genuine product, credential and deployment restrictions still apply.

The recovery agent must act on repairable operational gaps, including fixing a missing dispatch route under a valid infrastructure claim, rather than treating that missing route as an external blocker. Implement and test those changes under the claim, then hand them to normal PR review. Installation is permitted only through the reviewed merged-only path; never install unreviewed working-tree changes. It must inspect all affected queues; one blocked feature must not end the entire recovery pass.

The agent may find legitimate ongoing CI, quota exhaustion or an external decision rather than a recoverable fault. It must leave a specific next-check time instead of polling CI or inventing eligibility. It cannot bypass independent review, CI, security checks, deployment authorization, paid-capacity limits or wallet restrictions. The prompt explicitly prohibits account rotation and recursive recovery agents.

The timer itself uses no model quota. Each launched recovery session uses the configured Codex command and account, as other automation workers do.

## Session limits and receipts

Only one recovery session runs globally across the enabled projects. A separate observation lock serializes timer and manual invocations. Launch intent is written before spawning the model. PID/start-time receipts, the model process group and an inherited run nonce fence surviving descendants, including a helper that creates a new session. An uncertain launch or exit remains held for explicit inspection; it is never replayed automatically.

Default session runtime is 20 minutes. On timeout, the wrapper terminates and then kills its own child process group and records a failure. An escaped surviving helper still prevents another launch. Failed or malformed results receive a 30-minute cooldown. A successful result can request a later check, up to 24 hours, for a genuine external wait. A normal process exit alone does not prove recovery: the next observation separately records whether queue, worker or attention state actually changed.

Private receipts and logs live under:

```text
~/.local/state/pr-coordinator/recovery-watchdog/
```

`latest.txt` and `latest.json` contain the most recent supervision report. `state.json` preserves progress observations, cooldowns, current session and its result. Each `run-*` directory contains the prompt, sanitized diagnostic snapshot, command, process identities, private worker log, exit status and `result.json` written by the recovery agent. Reports contain actions, verification, remaining blockers and a Unix timestamp for the next check. The wrapper receives no inherited GitHub token or implementation claim token.

An uncertain receipt or surviving helper requires checking that process before clearing anything. Never delete the state file merely to get another agent started.

## Installation and configuration

Install only the additive watchdog component from the implementation checkout:

```bash
python3 tools/pr-coordinator/install_watchdog.py --project suifleet --enable
```

The installer stages a content-addressed watchdog generation, sets its own `current` link, installs its timer/service, and adds only `recovery_watchdog` to the existing private configuration. It preserves the full prior configuration in a private backup, rejects concurrent configuration changes and holds the runtime activation lock while installing. It does not replace or restart the review coordinator, dispatcher or their workers. Node's resolved directory is included in the service PATH for the configured Codex CLI. The service observation timeout allows 60 seconds for fleet status, 45 seconds per enabled project for ticket inventory, and 60 seconds of launch overhead. Reinstall after changing the enabled project list. Manual worker launches also use private file permissions.

Multiple projects are explicit opt-ins:

```bash
python3 tools/pr-coordinator/install_watchdog.py --project suifleet --project berth --enable
```

The private coordinator configuration accepts:

```json
"recovery_watchdog": {
  "enabled": true,
  "projects": ["suifleet"],
  "stall_seconds": 900,
  "cooldown_seconds": 1800,
  "timeout_seconds": 1200
}
```

Use the configured project IDs. The implementation does not create trackers or enable implementation/review slots for an unregistered repository. Unknown project IDs, nonboolean enable values and invalid bounds fail closed.

Observe the service and local report:

```bash
systemctl --user status recovery-watchdog.timer
journalctl --user -u recovery-watchdog.service -n 20 --no-pager
cat ~/.local/state/pr-coordinator/recovery-watchdog/latest.txt
```

A manual diagnostic pass without model execution:

```bash
python3 ~/.local/lib/pr-coordinator/recovery-watchdog/current/recovery_watchdog.py \
  --config ~/.config/pr-coordinator/config.json
```

For an explicitly requested immediate recovery pass, add `--execute --kick`. `--kick` skips only the initial observation grace. It does not bypass the singleton, cooldown, disabled flag or maintenance lock.

Pause new launches while continuing to observe an existing recovery session:

```bash
touch ~/.local/state/pr-coordinator/recovery-watchdog/disabled
```

Remove that file to allow launches again. To disable recurring observations entirely, disable `recovery-watchdog.timer`. The watchdog does not restart a deliberately disabled timer. Holding `~/.local/state/pr-coordinator/activation.lock` also prevents new recovery launches during maintenance.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s tools/pr-coordinator/tests -p test_recovery_watchdog.py -v
```

The offline tests exercise progress and idle detection, cooldown and genuine provider waits, duplicate timer calls, restart adoption, uncertain launch/exit, disabled/maintenance holds, recursion prevention, configuration validation and additive installation. A real subprocess test verifies timeout cleanup and durable exit evidence. Installation and a live agent launch are recorded separately in the issue handoff; unit tests never call GitHub or a real model.
