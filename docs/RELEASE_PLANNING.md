# Release planning and rehearsal (SF-154)

`scripts/release_plan.py` turns the release steps in
[OPERATIONS.md](OPERATIONS.md#deployment-process) into a reviewable plan for one
network. `scripts/publish.py --plan` then admits only the transaction the plan
describes. The same planner and publisher run on localnet, testnet and (for
planning only) mainnet.

This is preparation, not a production release. **No production release has
occurred.** Mainnet plans are read-only and unsigned:
- the policy forbids executing them;
- `scripts/publish.py` still refuses mainnet before it reads any plan;
- nothing here signs, submits, transfers an UpgradeCap or touches production secrets.

Actual mainnet execution stays in [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Release signing and device trust stay in [SF-042/#62](https://github.com/tyler-suifleet/suifleet/issues/62).

## What a plan pins

The planner only reads. It reads the chain over gRPC (`chain_facts`), plus this
checkout's sources, `Published.toml` and `deployments/ENV/`.

| Section | Contents and checks |
|---|---|
| `revision` | Commit; `contracts_source_digest` (SHA-256 over `Move.toml`, `Move.lock` and `sources/**/*.move`); and the release compiler's bytecode digest. With `--build-tool`, it compiles a scratch copy, so the checkout is never written. A build that would change `Move.lock` is blocked. Uncommitted sources are refused unless `--allow-dirty`, which blocks testnet/mainnet. |
| `chain_identifier` | Live chain. testnet must be `4c78adac` and mainnet `35834a8a`. A public chain cannot be labelled localnet/devnet. `Move.toml` must declare a local environment with the same identifier. |
| `custody` | The account that must own the UpgradeCap and sign. Blocks an unset (`0x0`) custody, and custody the policy forbids (the dedicated testnet agent and CI wallets may never hold mainnet custody). Transfer is never performed. |
| `deployment` | Upgrade only: the latest package and version, the UpgradeCap's package, version and owner, the registry's shared version, and every on-chain type origin. Blocks a stale cap (another upgrade chained elsewhere), a non-address-owned cap, custody mismatch, and records that disagree with `Published.toml` or the chain (`dm.verify`). |
| `compatibility` | Static checks against on-chain type origins. Blocks types or modules the sources dropped. Lists new types and newly introduced optional protocol groups, each with a migration review entry: existing objects gain no fields or dynamic fields automatically. The chain's upgrade verifier (dry run, rehearsal) remains authoritative for layouts and signatures. |
| `revision.cached_publication` | With `--fixture-state`, a testnet fixture whose verified bytecode equals this digest. Identical code already published, or already published by the current release, is blocked on public networks: reuse it, never republish. |
| `clients` | Client configs are generated from the recorded manifest into a scratch directory, with the client versions checked. |
| `cost` | Gas budget against the policy limits; estimate; storage is never refunded; exposure. |
| `execution` | `disposable` (localnet/devnet), `rehearsal` (testnet) or `forbidden` (mainnet), plus the exact dry-run and execute commands. A plan for an adopted deployment that this checkout does not record (`--package-id`) is planning only. |
| `rollout`, `abort_boundaries` | Policy rollout stages and halt conditions, and what can and cannot be undone at each phase (below). |
| `findings`, `ready` | `blocking` or `warning` findings. A plan is ready only without blocking findings. |
| `plan_digest` | SHA-256 over the canonical plan. An edited plan is refused. |

Limits live in [`manifests/release-policy.json`](../manifests/release-policy.json):
per-network execution mode, maximum gas budget, run spend, plan validity (24 h)
and forbidden custody. The loader refuses a policy that allows mainnet
execution.

## Admission: `scripts/publish.py --plan PLAN.json`

After its own chain/account/UpgradeCap checks, and before any transaction,
including `--dry-run`, the publisher refuses unless all of these hold:
- the plan is intact, ready, unexpired and allows execution;
- it names this network, environment, chain and active account;
- the action matches: a publish plan cannot run once a deployment exists;
- for an upgrade, the package, version, UpgradeCap and registry are unchanged since planning;
- the contract sources still have the planned digest;
- `--gas-budget` is within the planned budget.

Testnet publication requires `--plan`; a testnet dry run does not. The admitted plan is kept:
- in the pending record;
- in the release as `release-plan.json`, beside `transaction.json` (digest, effects status and gas used). `--recover` keeps both.

## Commands

```bash
# Read-only plan; exit 0 ready, 2 not ready, 1 error.
python3 scripts/release_plan.py plan --network testnet --rpc https://fullnode.testnet.sui.io:443 \
  --custody 0xCUSTODY --build-tool sui --out plan.json --markdown plan.md
python3 scripts/release_plan.py check plan.json
# Review plan.md, then (testnet/localnet only):
scripts/redeploy.sh --expect-address 0xCUSTODY --plan plan.json --gas-budget N --dry-run
scripts/redeploy.sh --expect-address 0xCUSTODY --plan plan.json --gas-budget N
```

Other options:
- `--package-id/--upgrade-cap/--registry` plan an adopted deployment without records.
- `--fixture-state` reads the testnet fixture cache (`live-state.json`, read-only) for identical bytecode.
- `--bytecode-digest` supplies a digest computed elsewhere.

## Abort and rollback boundaries

| Phase | Boundary |
|---|---|
| Before the transaction | Every check and plan refusal changes nothing and spends no gas. |
| Executed, failed effects | Gas is charged. Package, records, `Published.toml` and client configs are unchanged. |
| Executed, recording failed | The chain changed and cannot be rolled back. `pending-DIGEST.json` (with the plan) is kept. `--recover` records it without another transaction, and the prior configs stay active. |
| Activation interrupted | The journal rolls forward (`--reconcile`), and `make dapp-build` refuses until then. |
| After activation | Packages are immutable, and older versions stay callable, so there is no on-chain rollback. The superseded release is kept but rejected as stale, so recovery is a reviewed forward-fix upgrade under a new plan. |
| Rollout | Halt the current stage on any halt condition. Devices keep their journals and keys, and nothing forces a replay. |

## Tests

| Command | Covers |
|---|---|
| `make test-publish` | Offline (also runs `scripts/tests/test_release_plan.py`):<br>• fresh and upgrade plans, and admission through the real publisher with a fake CLI and chain<br>• replayed, superseded, tampered, expired, over-budget and wrong-account plans<br>• changed sources<br>• dropped types and new optional groups<br>• stale, foreign and non-address-owned caps; stale manifests; orphans<br>• identical bytecode, against the current release and the fixture cache<br>• scratch compilation, compiler failures and missing lock pins<br>• policy validation<br>• mainnet planning-only: forbidden execution, forbidden development custody, and publish.py's own refusal with or without a plan<br>• `--recover` keeping the plan |
| `make test-release-rehearsal-localnet` | Real localnet, contracts, CLI and both clients: plan, refusals with no write and no gas, publish under a plan, replay refused, a compatible upgrade adding a type (dry run, then execute) whose origin is the upgrade, the stale superseded release, dropped-type and edited plans refused, identical bytecode flagged, then the `scripts/identity_lifecycle_localnet.py` old-package attacks from the historical pre-identity package (`d3acb1e`, as in `test_move_security_localnet.py`) into the rehearsed head, on the same chain. `REHEARSAL_ARGS="--evidence FILE"` writes the evidence. |

The automatic testnet package-upgrade and upgrade-security gates
([TESTNET.md](TESTNET.md)) remain the public-network upgrade evidence for
each head. They reuse cached identical bytecode, verify effects and run the
old-package attacks.

## Rehearsal evidence (2026-10-25)

Code revision `195220e0ce945b98e882e961641d3f2c131e0e6d`; release compiler `sui 1.81.1-e04cf9aa915c`
(`suifleet-dev:sf037`). Full JSON: [`validation/sf154-release-rehearsal.json`](validation/sf154-release-rehearsal.json).
No mainnet or production transaction, signature, capability transfer or credential
operation occurred. No testnet state was mutated.

**Isolated localnet** (`--network none`, disposable chain `b565b295`): all seven steps passed.
- **Plans.** Fresh publish `sha256:700ccdc9…` and upgrade `sha256:30411e1d…`, both ready. Head bytecode `b6eededde08cb498a0bc06d5e52f61edd61aefc9b57403ad094814664f75114d`.
- **Refusals.** None wrote anything or spent gas:
  - another custody account;
  - a plan over the policy budget;
  - a gas budget above the plan's;
  - the replayed publish plan;
  - a plan whose sources dropped `release_rehearsal::RehearsalMarker`;
  - an edited plan.
- **Publish.** `CG8n8WUwrQuircaWTkajKTcHbPpSf2E7VhY9dzVE75kG` succeeded. Net 532,828,600 MIST, equal to the account's balance change.
- **Compatible upgrade.** Dry run, then `BnV7ZZ4NCAqCSzr8hhU2q9ynBz1hUJNaZ8NVxVXyb9Z9` succeeded. Net 536,150,620 MIST.
  - `RehearsalMarker` originates in v2.
  - Every other origin, the registry and the UpgradeCap are unchanged.
  - Daemon `--check-config` and the frontend manifest check accept v2 and reject v1 as stale.
- **Old-package attacks.** `identity_lifecycle_localnet.py` from `d3acb1e` into the rehearsed head: **47/47**.

**Read-only public plans** (gRPC reads only; nothing signed):
- **Testnet** (`4c78adac`): upgrade plan `sha256:f8370e1f…`, ready, planning only. It adopts the agent wallet's cached fixture: package `0x99ade699…` v2, UpgradeCap `0x05749c80…`, owned by the agent account, and registry `0x4431790c…`.
  - 60 on-chain type origins; no removed types.
  - 54 new types in six newly introduced optional groups, each listed for migration review.
- **Testnet, cached bytecode.** The same fixture planned with its own verified head digest `6e5abf07…` was blocked as `identical-bytecode`: already published at `0x99ade699…`.
- **Mainnet** (`35834a8a`): read-only fresh-publish plans, execution forbidden, neither ready:
  - `sha256:64c3e0f8…` is blocked by `custody-unset` and by `bytecode-build`: the repository's `Move.lock` has no mainnet dependency pins.
  - `sha256:71a82749…`, naming the testnet agent wallet, is blocked as `custody-forbidden`.

**Finding.** The current package costs about 0.53 SUI per publish or upgrade, almost all non-refundable storage. The policy therefore estimates 0.55 SUI, caps testnet/mainnet gas budgets at 0.8 SUI and run spend at 1 SUI. The earlier 0.5 SUI localnet harness budget was raised to 2 SUI.

## Outstanding (not run here)

- Actual mainnet publication/upgrade, UpgradeCap custody provisioning/transfer and mainnet end-to-end acceptance: SF-087/#159, only under explicit user authorization for that run. Committing mainnet dependency pins to `Move.lock` and choosing custody are prerequisites the mainnet plan reports.
- Production release signing environment, trust roots and device trust: SF-042/#62.
- Physical Pi/target rollout, reboot and power-loss acceptance: SF-031/#32.
- A mutating testnet execution of `publish.py --plan` against a recorded testnet deployment. None exists yet, and the automatic package-upgrade gate already exercises testnet upgrades with cached identical bytecode.
