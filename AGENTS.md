# SuiFleet agent instructions

## Ticket authority — user requirement

**Codex is the only agent allowed to create new tickets or issues unless the user explicitly authorizes another model to do so.** This includes GitHub issues, sub-issues, draft issues and tickets in another tracker. Assignment to implement or review an existing issue is not permission to create more issues. Claude and other models must report newly discovered work to Codex in the existing issue or their handoff. Do not bypass this rule through a script, API, delegated agent or renamed task.

An explicit user exception applies only to its stated model and scope. Humans retain control of the tracker. This is an agent workflow rule; shared GitHub credentials do not enforce model identity technically.

## Commit policy — user requirement

Keep commits small, contained and itemized. Each commit must have one clear purpose, stage only its intended paths/hunks, and use a descriptive subject with an itemized body explaining the change and verification. Separate unrelated functional changes, dependency updates, formatting, infrastructure and documentation. Keep coupled API/lockfile changes together when separating them would break the build. Do not sweep a mixed working tree into one commit.

**All commits must be authored by the user's account, never by a model.** Use the user's verified Git identity for both author and committer. The current identity is `Tyler Bush <tyler.jonathan.bush@gmail.com>`, associated with GitHub `therealtime`. GitHub CLI authentication as `tyler-suifleet` is separate from commit authorship. Preserve this identity unless the user explicitly changes it; if it is missing or cannot be verified, resolve that before committing.

Do not add model names as authors, committers, `Co-authored-by` trailers, sign-offs or other model-authorship attribution. Put model ownership/testing evidence in the existing issue or handoff instead. Verify the resulting commit metadata and staged scope. Committing does not imply permission to push, merge or deploy; follow the user's actual scope.

## Project and current state

SuiFleet targets functional AWS Greengrass OTA/fleet-management parity using Sui Move contracts, Walrus storage, a Next.js wallet app and a Rust Linux daemon. It currently implements a locally tested one-shot OTA foundation, not full Greengrass parity or a production release.

Read [the parity audit](docs/GREENGRASS_PARITY_AUDIT.md), [validation evidence](docs/VALIDATION.md), [the operational runbook](docs/OPERATIONS.md) and [tracking workflow](docs/ISSUE_TRACKING.md) as relevant. Actual code and current test results take precedence over historical checklists. The [capability-to-ticket map](docs/GREENGRASS_TICKET_COVERAGE.md) tracks the audited scope; [Raspberry Pi 3 Model B+ acceptance](docs/RASPBERRY_PI_TEST_PLAN.md) requires a BitBake-built bootable image with the client included, not only SSH deployment, and is specified in SF-029/030/031. SF-030 supplies the tested component/BitBake updater recipe; physical image and Pi acceptance remain SF-029/031.

Critical known gaps:

- The frontend uses pinned Sui SDK 2.36.0 and dApp Kit React 2.1.40 with gRPC-Web; see [frontend compatibility and wallet acceptance](docs/FRONTEND_CLIENT.md). The daemon and publishing resolver still use deprecated JSON-RPC; migrate the daemon reader, transaction builder and executor in SF-004. Real extension-wallet acceptance remains required for SF-003.
- Existing types keep their introduction-package origin; types introduced by later upgrades have later origins. The implemented deployment manifest records each origin, including `RecipeDigestKey`, `DeviceAddressKey` and authoritative receipts. Preserve this boundary during client/API changes.
- Older Sui package versions remain callable. SF-001 introduced isolated authoritative receipts and local-chain old-package attack/migration tests; public-network upgrade acceptance remains SF-010. Never trust legacy StatusesKey or assume an old package is disabled.
- Current unit tests and the HTTP simulator do not establish real chain upgrade compatibility, live wallet behavior, Walrus certification, Yocto builds or hardware OTA health.

## Infrastructure and ownership

| Path | Responsibility |
|---|---|
| `contracts/sources`, `contracts/tests` | Move devices/capabilities/groups/deployments and contract tests |
| `dapp/src` | Wallet UI, transaction builders, queries and Walrus uploads |
| `suifleet-edge/src` | Identity, chain adapter, recipes, updater and durable journal |
| `suifleet-edge/meta-suifleet` | Yocto recipe and systemd service |
| `scripts/redeploy.sh` | Development publish/upgrade helper; currently needs hardening |
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

`make shell` opens the container. Make detects `INSIDE_DEV_CONTAINER=1`; run the same targets there without nested Docker. The Dockerfile pins the Ubuntu base digest, Sui `testnet-v1.71.1` (checksum-verified), Rust `1.87.0`, Node `22.22.2` and pnpm `11.0.8`; these are the existing baseline, not a claim of compatibility with current public networks. Use locked dependencies. Deliberate dependency upgrades must update lockfiles and compatibility tests together.

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

Do not replace tests with type casts, ignored failures or mocked success. A returned digest does not prove transaction success; inspect execution effects. Record exact commands, results, limitations and changed behavior in the PR/handoff. Do not claim tests ran when they did not. [VALIDATION.md](docs/VALIDATION.md) is historical evidence and must not be treated as a current test run.

## Keeping clients and contracts synchronized

- Maintain one versioned deployment manifest per network with chain identity, call package ID, per-type origins, shared object IDs, protocol/schema versions and supported client ranges. This manifest is implemented; see [DEPLOYMENT_MANIFEST.md](docs/DEPLOYMENT_MANIFEST.md).
- Generate or centrally define Move calls/event/recipe schemas and test both clients against actual compiled interfaces.
- Preserve existing public function signatures, layouts and abilities for a compatible upgrade; use additive APIs and explicit migrations where needed.
- Do not assume an initializer reruns, objects migrate automatically, or an old package is disabled.
- Test mixed versions, missing fields, stale IDs, revoked/legacy caps, ownership/membership changes and attempts through old entry points.
- Reject unsupported protocol/recipe versions clearly before executing an updater. Recipe bytes and artifact bytes both require authenticated digests.

## Deployment

A task to edit code/docs or implement an issue does not itself authorize live publication or hardware programming. Follow the user's actual deployment scope; do not add another approval step if the session already authorizes that deployment. Never place keys, recovery phrases or tokens in source, issues, logs or PRs.

The current `make publish-contracts` runs `scripts/redeploy.sh`, which spends gas and mutates chain state. It only writes frontend `.env.local`; it does not synchronize device configuration. It also has unscoped metadata parsing and error-suppression risks. Do not use it as a production release pipeline until those issues are fixed.

A release must verify the target chain/account, run relevant checks, prepare a compatibility/migration plan, publish or upgrade under the proper UpgradeCap, validate execution effects, record all IDs/type origins, migrate required objects, generate both client configurations, rebuild the static frontend, stage the daemon rollout, and run a real end-to-end acceptance test. Full details and current blockers: [OPERATIONS.md](docs/OPERATIONS.md).

## Working across models

**On this WSL machine, claim implementations through [the atomic ticket coordinator](docs/ATOMIC_TICKETING.md) before editing.** Both Codex and Claude use the shared installed `ticket` CLI, central WSL database, committed `.atomic-ticketing.json` and this project's registered repository/linked worktrees. SuiFleet and Vortech have separate namespaces, API accounts and unchanged SSH aliases; shared hardware/build/port resources can be reserved across projects. GitHub is the specification tracker; the central SQLite database is authoritative for implementation ownership. Do not claim by directly changing Project fields.

For “implement the next ticket”: run `ticket --project suifleet refresh`, then `ticket --project suifleet claim --agent Codex --model ACTUAL_MODEL --session UNIQUE_SESSION` (Claude substitutes its own family/model/session). Enter the returned worktree. Claiming filters priority, completed dependencies, live ownership atomically. File paths are not reserved: edit whatever the ticket needs; overlapping changes are resolved at review/merge. Preserve the returned token locally and heartbeat every five minutes. Run `assert TICKET --token TOKEN` after long commands and before staging/committing/deploying to confirm the claim is still yours. Use the installed CLI with `--project suifleet` when a worktree contains an older catalog. Resource labels on claims are descriptive and do not reserve hardware. Acquire a separate `ticket lock` only immediately before actually using shared hardware/build/port resources; verify with `lock-assert`, renew during use, and `unlock` afterward. Developing code, building the client/image and running simulators do not require a Raspberry Pi lock. Review or blocked tickets do not retain hardware locks. See the coordinator runbook for commands.

Read coordinator `list`/`show` and record testing plans, shared-interface decisions and handoffs with `note`. Review and blocked work keep their claim; review is not Done. An expired claim remains reserved: stop the old worker before an explicit recovery, and never edit with an invalid token. Mirror failures retain claims; inspect and retry `publish`, then verify ownership before proceeding. Integrate reviewed changes serially. The coordinator does not create issues, merge/push/deploy, restart models, or change permission settings; follow existing authorization. Claims are cooperative, not an OS barrier against a model ignoring these instructions.

Work from an existing assigned issue. Record the agent family and actual model/version, branch/worktree, dependency issues, changed areas and testing plan. GitHub assignees are human accounts; the Project's `Agent` field tracks model ownership. No issue should have two implementation owners simultaneously.

Use one branch/worktree per issue when separate models work concurrently. When parallel work touches the same shared interfaces (contracts, transaction helpers, lockfiles, manifests, Makefile), agree on the interface in notes/issues; no file reservation is needed. Do not start a dependency-blocked change by guessing another model's API. Coordinate the interface first.

Any model may implement/review assigned issues and report progress under the user's authorization. Only Codex creates new tickets unless explicitly excepted. Before handing off, include changed files, exact verification results, remaining risks, migration requirements and the next concrete step. Mark work Review when evidence is ready; do not mark Done merely because code compiles or local simulator tests pass when the issue requires live/hardware validation.
