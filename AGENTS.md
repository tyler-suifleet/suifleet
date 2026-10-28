# SuiFleet agent instructions

## Ticket authority — user requirement

**Codex is the only agent allowed to create new tickets or issues unless the user explicitly authorizes another model to do so.** This includes GitHub issues, sub-issues, draft issues and tickets in another tracker. Assignment to implement or review an existing issue is not permission to create more issues. Claude and other models must report newly discovered work to Codex in the existing issue or their handoff. Do not bypass this rule through a script, API, delegated agent or renamed task.

An explicit user exception applies only to its stated model and scope. Humans retain control of the tracker. This is an agent workflow rule; shared GitHub credentials do not enforce model identity technically.

## Capture conversation findings — user requirement

Codex must capture actionable bugs, missing features, changed requirements and acceptance gaps discovered in conversations as tracker work before handing off the turn. First check for an existing issue: update its specification/acceptance rather than create a duplicate. Publish genuinely new work to GitHub Issues and the Project, keep the seed backlog/publication records synchronized, and report the issue links. Publish and verify remote IDs before introducing a new seed entry so other models never fail refresh on an unpublished ticket. Preserve existing ownership and meaningful dependencies; reporting a finding does not authorize stealing its implementation.

Distinguish confirmed defects, implementation requirements, outstanding acceptance and speculative business/design ideas. Keep unapproved opportunities in the business/design documents rather than silently turning them into implementation commitments. Other models still report findings through the existing issue/handoff for Codex to triage; the Codex-only creation rule remains unchanged. Do not leave a concrete discovery only in chat or mark it fixed merely because a ticket was created.

## Commit policy — user requirement

Keep commits small, contained and itemized. Each commit must have one clear purpose, stage only its intended paths/hunks, and use a descriptive subject with an itemized body explaining the change and verification. Separate unrelated functional changes, dependency updates, formatting, infrastructure and documentation. Keep coupled API/lockfile changes together when separating them would break the build. Do not sweep a mixed working tree into one commit.

**All commits must be authored by the user's account, never by a model.** Use the user's verified Git identity for both author and committer. The current identity is `Tyler Bush <tyler.jonathan.bush@gmail.com>`, associated with GitHub `therealtime`. GitHub CLI authentication as `tyler-suifleet` is separate from commit authorship. Preserve this identity unless the user explicitly changes it; if it is missing or cannot be verified, resolve that before committing.

Do not add model names as authors, committers, `Co-authored-by` trailers, sign-offs or other model-authorship attribution. Put model ownership/testing evidence in the existing issue or handoff instead. Verify the resulting commit metadata and staged scope. Committing does not imply permission to push, merge or deploy; follow the user's actual scope.

## Project and current state

SuiFleet targets functional AWS Greengrass OTA/fleet-management parity using Sui Move contracts, Walrus storage, a Next.js wallet app and a Rust Linux daemon. It currently implements a locally tested one-shot OTA foundation, not full Greengrass parity or a production release.

Read [the parity audit](docs/GREENGRASS_PARITY_AUDIT.md), [validation evidence](docs/VALIDATION.md), [the operational runbook](docs/OPERATIONS.md) and [tracking workflow](docs/ISSUE_TRACKING.md) as relevant. Actual code and current test results take precedence over historical checklists. The [capability-to-ticket map](docs/GREENGRASS_TICKET_COVERAGE.md) tracks the audited scope; [Raspberry Pi 3 Model B+ acceptance](docs/RASPBERRY_PI_TEST_PLAN.md) requires a BitBake-built bootable image with the client included, not only SSH deployment, and is specified in SF-029/030/031. SF-030 supplies the tested component/BitBake updater recipe; physical image and Pi acceptance remain SF-029/031.

Yocto packaging/first boot is described in [YOCTO_PACKAGING.md](docs/YOCTO_PACKAGING.md). Use the pinned Scarthgap/Rust-mixin and Wrynose profiles under `kas/`, preserve the fixed daemon source revision and regenerate the crate include/lock digest together when updating it. Provision device identities only at runtime; package/image builds must contain no device keys, enrollment configuration or journals. QEMU test profiles enable passwordless console login and are for disposable validation only. Record physical Pi acceptance separately.

Critical known gaps:

- The frontend uses pinned Sui SDK 2.36.0 and dApp Kit React 2.1.40 with gRPC-Web; see [frontend compatibility and wallet acceptance](docs/FRONTEND_CLIENT.md). The daemon uses the supported gRPC v2 API through Mysten's Rust SDK (SF-004): event discovery, reads, local transaction building with a verify-before-sign check, and effects-checked execution; it needs a node serving indexed `ListEvents` (Sui ≥ 1.81 in our tests). The publishing resolver and `scripts/publish.py` read the chain over gRPC too (SF-006). Real extension-wallet acceptance remains in SF-010/SF-031 after the SF-003 implementation merge.
- Existing types keep their introduction-package origin; types introduced by later upgrades have later origins. The implemented deployment manifest records each origin, including `RecipeDigestKey`, `DeviceAddressKey` and authoritative receipts. Preserve this boundary during client/API changes.
- Older Sui package versions remain callable. SF-001 introduced isolated authoritative receipts and local-chain old-package attack/migration tests; public-network upgrade acceptance remains SF-010. Never trust legacy StatusesKey or assume an old package is disabled.
- Current unit tests and the HTTP simulator do not establish real chain upgrade compatibility, live wallet behavior, Walrus certification, Yocto builds or hardware OTA health.

## Infrastructure and ownership

| Path | Responsibility |
|---|---|
| `contracts/sources`, `contracts/tests` | Move devices/capabilities/groups/deployments and contract tests |
| `interfaces`, `scripts/generate_interfaces.py` | Reviewed ABI/recipe schemas and generated TypeScript/Rust interfaces; see [shared interfaces](docs/SHARED_INTERFACES.md) |
| `dapp/src` | Wallet UI, transaction builders, queries and Walrus uploads |
| `suifleet-edge/src` | Identity, chain adapter, recipes, updater and durable journal |
| `suifleet-edge/meta-suifleet` | Yocto recipe and systemd service |
| `suifleet-edge/packaging/macos` | macOS LaunchDaemon plist and installer; see [MACOS_RUNTIME.md](docs/MACOS_RUNTIME.md) |
| `scripts/redeploy.sh`, `scripts/publish.py` | Verified publish/upgrade; records releases and generates both client configs |
| `scripts/test_ota.py` | Real daemon against local chain/Walrus simulators |
| `Dockerfile`, `docker-compose.yml`, `Makefile` | Development environment and build/test entry points |
| `docs/tracking/backlog.json` | Codex-maintained seed backlog; not evidence of published issues |
| `docs/tracking/github-state.json` | Created only after successful tracker publication; remote IDs/URLs |

Compose mounts the checkout at `/workspace` and exposes the dApp on port 3000. Named volumes retain Rust/Move/pnpm caches and Sui development wallet configuration. Runtime device paths are `/etc/suifleet/config.toml`, `/etc/suifleet/device.key`, and `/var/lib/suifleet`. Preserve journals and keys.

## Setup and build

From the host, use Docker and GNU Make:

```bash
make dev
make dapp-install
make build-contracts
make edge-build
make dapp-build
```

`make shell` opens the container. Make detects `INSIDE_DEV_CONTAINER=1`; run the same targets there without nested Docker. The Dockerfile pins the Ubuntu base digest, Sui `testnet-v1.81.1` (checksum-verified; same build as mainnet 1.81.1), Rust `1.94.0` (the lowest Rust in the supported Yocto LTS releases), Node `22.22.2` and pnpm `11.0.8`; these are the existing baseline, not a claim of compatibility with current public networks. Use locked dependencies. Deliberate dependency upgrades must update lockfiles and compatibility tests together.

Do not run `make down` casually: it deletes named volumes, including Sui wallet configuration. Do not reset, clean, overwrite, stage or commit unrelated work. Inspect `git status` before editing; existing work may belong to another agent or the user.

## Testing — required evidence

Use the smallest meaningful checks for a scoped change; use the full local suite for cross-layer changes and release candidates. Documentation-only changes need command/link consistency checks, not another complete application build.

```bash
make test-contracts
make edge-test
make test-ota
make dapp-typecheck
make dapp-build
# Equivalent full local suite, after dependencies are installed:
make check
```

`make test-ota` builds the debug daemon before running the simulator. A release build alone is not a substitute. Direct commands and failure-path expectations are in [OPERATIONS.md](docs/OPERATIONS.md).

| Change | Minimum verification |
|---|---|
| Move authority, layouts, API or dynamic fields | Move tests plus old-package/new-package and existing-object upgrade tests on a supported test network |
| Chain transport, signing, recipes, journal or targeting | Rust tests/build plus OTA simulator; independent signing vectors; real chain transaction/effects checks for transport changes |
| Wallet, SDK, transaction helpers or UI | Frontend typecheck and production build; wallet acceptance test for connect/sign/network/error flows |
| Updater/recovery | Simulator failure/restart tests; hardware reboot/power-loss/rollback validation before claiming firmware safety |
| Packaging/toolchain | Rebuild the relevant image or Yocto recipe and record exact versions |
| Tracking bootstrap | Offline tracking tests and dry run; no remote mutation in tests |

Normal CI also runs CodeQL and dependency/secret/configuration security gates; see [SECURITY_CI.md](docs/SECURITY_CI.md). Keep raw scanner reports private and publish only sanitized diagnostics. Do not suppress findings or treat a successful scanner process as a clean result.

Do not replace tests with type casts, ignored failures or mocked success. A returned digest does not prove transaction success; inspect execution effects. Record exact commands, results, limitations and changed behavior in the PR/handoff. Do not claim tests ran when they did not. [VALIDATION.md](docs/VALIDATION.md) is historical evidence and must not be treated as a current test run.

## Platform update boundary — user requirement

SuiFleet delivers authenticated artifacts, executes explicitly configured component lifecycle commands and records their reported results. Platform scripts/tools (for example SWUpdate or RAUC integrations) own updater selection, OS installation, partition/bootloader layout, reboot decisions, boot confirmation and firmware rollback. Do not add automatic updater detection, SuiFleet-triggered system reboot, a firmware boot/recovery state machine or mandatory A/B partitioning as Greengrass-parity requirements. Preserve SuiFleet journals and component state across platform-controlled restarts; do not infer healthy boot from command exit zero or replay ambiguous destructive work. Component process supervision and explicitly configured application recovery remain separate runtime responsibilities. Platform-specific recovery tests establish only the tested integration.

## Keeping clients and contracts synchronized

- Maintain one versioned deployment manifest per network with chain identity, call package ID, per-type origins, shared object IDs, protocol/schema versions and supported client ranges. This manifest is implemented; see [DEPLOYMENT_MANIFEST.md](docs/DEPLOYMENT_MANIFEST.md).
- Use `make generate-interfaces` and `make check-interfaces` for the reviewed ABI and recipe schemas in `interfaces/`; follow [SHARED_INTERFACES.md](docs/SHARED_INTERFACES.md).
- Preserve existing public function signatures, layouts and abilities for a compatible upgrade; use additive APIs and explicit migrations where needed.
- Do not assume an initializer reruns, objects migrate automatically, or an old package is disabled.
- Test mixed versions, missing fields, stale IDs, revoked/legacy caps, ownership/membership changes and attempts through old entry points.
- Reject unsupported protocol/recipe versions clearly before executing an updater. Recipe bytes and artifact bytes both require authenticated digests.

## Deployment

A task to edit code/docs or implement an issue does not itself authorize live publication or hardware programming. Follow the user's actual deployment scope; do not add another approval step if the session already authorizes that deployment. Never place keys, recovery phrases or tokens in source, issues, logs or PRs.

`make publish-contracts` runs `scripts/redeploy.sh`, which spends gas and mutates chain state (use `--dry-run` first). It verifies chain identity, account and UpgradeCap before the transaction and stops on failed effects. It records the release under `deployments/ENV/` and generates both client configs; see [DEPLOYMENT_MANIFEST.md](docs/DEPLOYMENT_MANIFEST.md). It refuses mainnet and has not been run against testnet; it is not yet a production release pipeline.

A release must verify the target chain/account, run relevant checks, prepare a compatibility/migration plan, publish or upgrade under the proper UpgradeCap, validate execution effects, record all IDs/type origins, migrate required objects, generate both client configurations, rebuild the static frontend, stage the daemon rollout, and run a real end-to-end acceptance test. Full details and current blockers: [OPERATIONS.md](docs/OPERATIONS.md).

## Testnet accounts

The user authorized agents to spend **testnet** SUI from two dedicated accounts on acceptance runs, such as publishing and upgrading test packages ([TESTNET.md](docs/TESTNET.md)).
- **Machine sessions:** the `agent` wallet, `0x0e499424…6965`, at `~/.config/suifleet/testnet/agent/client.yaml`.
- **CI:** the `ci` wallet, `0x99894c1a…dcac`, whose key exists only as a GitHub environment secret.

**Rules:**
- Testnet only.
- Never print, log or commit keys or recovery phrases.
- Pass the wallet's `client.yaml` explicitly.
- Public testnet is normal automated relevant validation, with standing authorization and no approval per run. Preserve preflight and the strict non-replenishing spending allowance (including failed transactions and device gas).
- Record digests and evidence in the PR or issue.

**Funding.** Use `sui client faucet --address …` with Sui ≥ 1.82.1, which solves a proof of work. The pinned 1.81.1 compiler CLI cannot.

**Two CLIs.** Live runs compile with the pinned release compiler (`sui`, 1.81.1). They read, sign and submit with the checksum-pinned transaction CLI at `/opt/sui-network/sui` (1.82.1), which must support the network's protocol version (138 and 139 respectively). Package bytecode is verified in the unsigned transaction before signing. See [TESTNET.md](docs/TESTNET.md#toolchain-release-compiler-and-transaction-cli).

**No JSON-RPC.** Public fullnodes no longer serve JSON-RPC, so live checks must use gRPC or the Sui CLI.

**Keep live runs cheap.** The faucet should be a rare top-up, not a per-run step. These were measured on testnet; details are in [TESTNET.md](docs/TESTNET.md#cost-findings):
- **Package storage is never refunded.** Publishing or upgrading this package costs about 0.087 SUI each time, which made up about 95% of an unoptimized run.
  - Reuse published packages. Cache fixtures by a content hash of the contracts, and re-verify them on chain (free reads) before reuse.
  - Never republish identical code.
- **Object storage is rebated** (about 99%) when an object is deleted. Delete transient objects (deployments, groups) at the end of a run.
- **Every transaction pays a minimum computation fee** (about 0.001 SUI). Batch calls into one PTB.
- **One PTB cannot call two versions of the same package** (`InvalidLinkage`). Use one transaction per package version.
- **The gas budget is not the cost, but it must not exceed the balance.** Gas selection fails before execution otherwise, and a failed execution still charges. Keep budgets near real cost (0.4 SUI is ample for a publish).
- **Upgrades only chain from the latest version.** To test a base→head upgrade, key the fixture by the (base, head) pair. Never test X→Y in place of base→Y.
- **Reuse funded test identities.** For example, keep the test device's key and gas, and top it up only when low.
- **Record each transaction's net gas** in the run's evidence. A cached package-upgrade run costs about 0.0025 SUI; a fresh fixture whose base and head differ costs about 0.41 SUI.

CI uses a main-sourced controller, successful originating CI and server-side write-authority/exact-head checks before executing trusted same-repository revisions; forks remain offline. Dedicated testnet environment protection is main-only without reviewers. Never weaken live-wallet, live-hardware or release protections. All CI testnet paths share one serialized wallet concurrency group. Cache device signing state only as authenticated ciphertext derived from the protected CI secret; plaintext belongs only in a private temporary runtime directory and never in public caches/artifacts. Evidence contains no keys.

Machine sessions must acquire/assert/renew `network:suifleet-testnet` only during shared-wallet mutations, coordinate in #11/#63, and unlock promptly. Funding is automatic when needed, at most one faucet request per run through the checksum-pinned transaction CLI; faucet credits and storage refunds never replenish the run allowance. Testnet availability does not add external-acceptance blockers to unrelated implementation tickets.

**Mainnet** and any other account still need explicit user authorization.

## Working across models

**On this WSL machine, claim implementations through [the atomic ticket coordinator](docs/ATOMIC_TICKETING.md) before editing.** Both Codex and Claude use the shared installed `ticket` CLI, central WSL database, committed `.atomic-ticketing.json` and this project's registered repository/linked worktrees. SuiFleet and Vortech have separate namespaces, API accounts and unchanged SSH aliases; shared hardware/build/port resources are locked globally across projects, only while in use. GitHub is the specification tracker; the central SQLite database is authoritative for implementation ownership. Do not claim by directly changing Project fields.

For “implement the next ticket”: run `ticket --project suifleet refresh`, then `ticket --project suifleet claim --agent Codex --model ACTUAL_MODEL --session UNIQUE_SESSION` (Claude substitutes its own family/model/session). Enter the returned worktree. Claiming filters priority, completed dependencies and live ownership atomically. Neither file paths nor hardware block a claim: edit whatever the ticket needs; overlapping changes are resolved at review/merge. Preserve the returned token locally and heartbeat every five minutes. Run `assert TICKET --token TOKEN` after long commands and before staging/committing/deploying to confirm the claim is still yours. Use the installed CLI with `--project suifleet` when a worktree contains an older catalog. Take shared hardware/build/port resources (for example `hardware:raspberrypi3bplus`) with `ticket --project suifleet lock NAME --agent … --model … --session … --purpose …` only when you start using them; `lock-assert` before each hardware operation, `lock-renew` every five minutes and `unlock` as soon as you finish. Never hold a lock across review or idle time.

Read coordinator `list`/`show` and record testing plans, shared-interface decisions and handoffs with `note`. Review and blocked work keep their claim; review is not Done. An expired claim remains reserved: stop the old worker before an explicit recovery, and never edit with an invalid token. Mirror failures retain claims; inspect and retry `publish`, then verify ownership before proceeding. Integrate reviewed changes serially. The coordinator does not create issues, merge/push/deploy, restart models, or change permission settings; follow existing authorization. Claims are cooperative, not an OS barrier against a model ignoring these instructions.

Work from an existing assigned issue. Record the agent family and actual model/version, branch/worktree, dependency issues, changed areas and testing plan. GitHub assignees are human accounts; the Project's `Agent` field tracks model ownership. No issue should have two implementation owners simultaneously.

Use one branch/worktree per issue when separate models work concurrently. When parallel work touches the same shared interfaces (contracts, transaction helpers, lockfiles, manifests, Makefile), agree on the interface in notes/issues; no file reservation is needed. Do not start a dependency-blocked change by guessing another model's API. Coordinate the interface first.

Any model may implement/review assigned issues and report progress under the user's authorization. Only Codex creates new tickets unless explicitly excepted. Before handing off, include changed files, exact verification results, remaining risks, migration requirements and the next concrete step. Mark work Review when evidence is ready; do not mark Done merely because code compiles or local simulator tests pass when the issue requires live/hardware validation.

Unattended reciprocal reviews and feedback repair are coordinated by [the PR service](docs/PR_COORDINATION.md). Leave merges to its single orchestrator while it is enabled. Review approval applies only to the exact head/base/feedback snapshot and complete ticket acceptance. When `PR_COORDINATOR_ACTIVE=1`, perform only the assigned review or feedback job; do not start another coordinator/model worker. A feedback worker inherits its existing implementation claim and must assert it before edits/commits. Only the orchestrator marks a verified merged ticket Done. Hardware/live acceptance requirements and the Codex-only issue authority remain in force.

User merge-completion policy: owned implementation PRs must include `Closes #ISSUE`, not only `Refs`. A verified merge closes the linked GitHub implementation issue and reconciles the atomic ticket and Project to Done, including external merges and retrying missed notifications. Preserve unfulfilled live/hardware validation in an acceptance ticket or follow-up; issue closure does not establish those test results. The PR service's automatic merge acceptance gates still apply.

Automatic implementation dispatch is described in [PR coordination](docs/PR_COORDINATION.md). When enabled, `ticket-dispatcher.service` refreshes and atomically claims the next eligible ticket for an available configured model slot. Workers must finish their assigned implementation, provide evidence, hand off to Review and exit; they must not claim successors themselves. Failed or uncertain workers retain reservations. Verified stopped workers may be parked/quarantined to free execution capacity for unrelated tickets; their existing ticket/PR remains reserved for explicit recovery. Missing process identity or surviving worker subprocesses still consume capacity. Review/merge checks and deployment authorization still apply.

The [recovery watchdog](docs/RECOVERY_WATCHDOG.md) checks progress every five minutes and starts one bounded Codex recovery session after a sustained stall in an explicitly enabled project. It complements the dispatcher and single merge coordinator. When `SUIFLEET_RECOVERY_ACTIVE=1`, inspect and recover authorized operations, verify the result and write the requested local report, then exit; never launch another recovery agent or recurring watcher. Recover expired ownership only after verifying the original worker and same-claim helpers stopped. Existing claim, independent review, CI, testnet spending and production/hardware authorization rules remain in force. Legitimate CI/provider waits require an explicit next-check time rather than idle polling. Reports and private launch receipts live under `~/.local/state/pr-coordinator/recovery-watchdog/`.

User-authorized implementation/integration acceptance split: follow [IMPLEMENTATION_ACCEPTANCE.md](docs/IMPLEMENTATION_ACCEPTANCE.md) and its per-ticket tracked scope. Reviewed implementations may merge before the explicitly deferred Pi, real-wallet, public-network or production-release integration checks. Keep code findings, local compatibility/regression evidence and CI as merge gates; retain the linked acceptance tickets and never claim deferred tests ran.

The user explicitly authorizes automatic public Sui testnet acceptance using the dedicated testnet agent/CI wallets described in PR #63 and SF-010/#11. Verify chain/account/authority, preserve keys, honor run spend limits and acquire `network:suifleet-testnet` only during machine-session mutations; coordinate with Claude before using shared state. Mainnet, other wallets, physical programming and production credential operations remain outside this grant. Testnet availability does not reinstate blanket external-acceptance merge blockers.


## Physical target acceptance — current user direction

Physical Pi work must not block software implementation, review or merge. The user explicitly permits resolving target issues later. Apply this rule to future tickets too: physical boot/networking, target reboot/power-loss, hardware-only isolation/performance and device-specific integration stay in SF-031/#32. The unfinished refreshed SD image SF-066/#111 is on hold; do not require that image for an offline software harness or unrelated feature. Do not claim a new image or physical result exists.

Preserve real software/API dependencies, source/security review, meaningful unit/simulator/host integration checks and normal authorized automatic testnet CI. This deferral grants no hardware programming authority and does not waive failing software tests. Packaging/image work must still produce actual artifacts before its own completion; an on-hold target ticket is not Done. Platform scripts remain responsible for reboot and firmware recovery.

## Standing authorization for continuous development

Keep eligible implementation, reciprocal review, feedback repair and gated integration
moving without asking the user to say "continue" or restart workers. The installed
services own successor dispatch and serial merges; individual workers finish their
assigned task and exit. Recover verified transient failures within the existing
claim, reconcile uncertain remote outcomes and preserve reservations when a worker
might still be alive. A real blocker pauses only its affected ticket: record the
reason and next action in the tracker/local status report, then use other eligible
capacity. Never manufacture work, tests or approvals to fill slots.

The user explicitly permits **physical-hardware and actual-mainnet testing to wait
until software development is complete**. Apply the shared development defaults in
`docs/tracking/acceptance-policy.json` to current and future software tickets, even
when an older issue or review bundles implementation with those checks. Keep
SF-031, SF-042, SF-066 and SF-087 open for their own acceptance; add future
acceptance-only tickets to the explicit exclusion list. Do not mark deferred checks
passed or delete genuine software dependencies. Mainnet writes and physical
programming remain unauthorized unless the user later explicitly authorizes them.

Local/simulator/isolated-chain tests, relevant automatic public-testnet validation,
security findings, exact-head CI and independent review remain required. The latest user direction (2026-10-21) also defers final production-provider and
manual real-wallet acceptance until implementation is complete. Keep production
Seal provider selection/authentication/fees/availability/recovery validation in
SF-087/#159, and real IdP/prover/Slush/extension-wallet acceptance in SF-031/#32
and SF-087/#159. Software-facing authentication, encryption, authority, session,
network-mismatch and failure-path checks remain implementation gates. This grants
no production credential provisioning, service purchases or mainnet writes. Maintain the existing periodic local status report with feature
progress, verified active workers and honest blockers. Services continue while WSL
is running; a stopped host is not evidence that unattended work is still executing.
