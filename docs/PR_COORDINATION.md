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

## Ownership and review loop

Only same-repository PRs whose head branch exactly matches a centrally reserved ticket are dispatched. Unknown ownership, draft PRs, blocked/in-progress tickets and missing model identity are reported and left alone. An active implementation is never resumed by a second worker. A feedback worker can resume only its existing family's published Review claim, with its unchanged token and clean worktree. The coordinator renews a running feedback claim; workers must also assert/heartbeat before edits and commits.

SQLite transactions reserve one job per PR and one worker per model family across configured projects. Reviews use an isolated detached worktree of the fetched exact head. They read all issue comments/formal reviews and review threads plus the ticket acceptance/evidence. A thread exceeding the implemented 100-comment limit stops the scan, rather than silently truncating feedback. No path or hardware is reserved for development. Workers acquire shared build/port/hardware resources only when actually using them, under the existing rules.

The worker writes a structured result containing its verdict, actual model, acceptance completeness and exact verification evidence. The engine publishes that real result on the PR, optionally using the configured independent GitHub reviewer account for a formal review at the specific commit. A changes-requested result queues the implementation owner's repair worker. Repairs push scoped Tyler-authored commits, reply to feedback and return the ticket to Review; the changed head is then independently reviewed again.

A reviewer must check existing change requests and acceptance, not infer approval from CI. Missing hardware/live evidence required by the ticket produces a blocked result. Worker mode does not authorize contract publication, deployment, hardware programming, issue creation or recursive worker launches. An expired ownership token is never recovered automatically.

## Merge gates and completion

The service requires:

- Open, ready, mergeable PR with published Review ownership and no unresolved threads.
- Approval from the other model family for the exact head, base commit and current feedback snapshot, with complete acceptance evidence.
- Every reported CI/status check explicitly successful; missing, pending, failed, skipped or cancelled checks block integration.
- No remaining formal GitHub change request from any reviewer.

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
  {"project": "suifleet", "family": "Codex", "model": "gpt-6.1-sol", "enabled": true},
  {"project": "suifleet", "family": "Claude", "model": "claude-opus-5-5", "enabled": true}
]
```

Use the actual installed worker model; these are recorded identities, not model
selection flags. Workers must report their actual running version in evidence.
One slot per project/family is supported. New installations have no enabled slots.
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

For an existing installation, add `--dispatcher-only` to preserve the installed
review coordinator and worker while updating only the dispatcher.

The service polls every 60 seconds, with durable exponential idle/pre-claim failure
backoff from 60 to 900 seconds. A failed fetch before a claim is retried; uncertain
claim or worker failures still retain ownership and stop the slot. A process lock serializes scans; durable private
SQLite receipts are in `~/.local/state/pr-coordinator/dispatch.sqlite3`, with worker
logs and process/exit receipts under `implementations/`. `--status` prints no tokens
and performs no GitHub operations. Without `--execute`, a scan performs no actions.
Tests run offline with `python3 -m unittest discover -s tools/pr-coordinator/tests`.

An active or blocked manual claim occupies its family's implementation slot even
if its lease expired. Active/retrying/orphaned PR review and repair jobs get priority
over new dispatch for their family. Existing work is not preempted. A worker starts
only after a successful refresh, fetch, atomic claim, published ownership and assert.
It inherits only the configured project's account token and existing claim; SSH
remotes remain unchanged. It heartbeats its own claim, implements and tests, pushes
small user-authored commits and opens a linked PR. It writes a `review_ready` receipt
and exits after handing off to Review. Only a stopped worker with an exit-zero
receipt, published Review/Done ownership and recorded evidence frees the slot.
This allows another independent implementation while the previous PR is reviewed.

Interrupted claims, failed launches, lost ownership or missing/failed receipts stop
that slot and retain the ticket reservation. Restarts adopt live process identities
using PID plus process start time; they never steal expired claims or restart an
uncertain implementation. To recover, stop and inspect the original worker, preserve
its worktree and evidence, then follow `docs/ATOMIC_TICKETING.md`. The dispatcher has
no automatic reset command: after explicit ticket recovery, an operator must archive
or update the corresponding private dispatcher receipt before enabling that slot.

Set a slot's `enabled` to `false` and restart the service to prevent its next claim,
or run `systemctl --user disable --now ticket-dispatcher.service` to stop dispatching.
Existing workers survive service stops; inspect them before any recovery. User
services run while this WSL instance is running; this does not keep Windows or WSL
awake. Watch service status and receipts for authentication, quota and external
acceptance blockers. Automated dispatch does not grant public-chain deployment,
physical hardware programming, release credential provisioning or issue-creation
authority to another model.

Implementation acceptance follows [the user-authorized split](IMPLEMENTATION_ACCEPTANCE.md). Its configured scope is included in each review fingerprint and worker snapshot; changing it requires fresh independent review and does not bypass source findings or CI. Claim tokens are stored only in the private local dispatcher SQLite database to allow restart ownership checks; GitHub tokens are never persisted there. Review and implementation slots are separate, so one family may briefly run an implementation and a review worker together.
