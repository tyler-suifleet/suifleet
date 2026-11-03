# SF-200 delay and cancel-window evidence

Implementation: Codex, configured/running model ID `gpt-6.1-sol`; no finer model
build identifier is exposed. Session `dispatch-434191834a3a4e43971461cc872bfc7e`,
branch `work/suifleet/sf-200-1fb34885`. Dependency SF-196 is merged. This is
implementation evidence, not independent approval or production acceptance.

## Behavior and compatibility

The optional delay factor is bit 8, off by default. Seven class durations accept
1,000–518,400,000 ms. Each execution checks immutable chain-clock queue creation,
current policy generation/proposer authority, exact canonical intent and expiry.
A private transaction-bound elapsed-time grant makes secondary class checks
honor their own duration. Settings/weakening/recovery use the current union's
maximum delay; changing settings invalidates old authorizations. Cancellation by
any current administrator is immediate. Pause skips only deployment timing;
resume and all other protected actions retain their requirements.

Existing Customer, Policy and Action layouts/signatures/origins are unchanged.
New timing/configuration/public-argument fields use a separate optional manifest
origin group. Old-package direct writes fail under active policy. Old-package
queues have no trusted timestamp and fail even after time advances. The console
configures and queues settings edits, verifies public arguments against on-chain
intent, shows advisory countdowns, cancels all discovered proposals and executes
mature settings edits. Other API-originated operations execute through their
originating client and exact secured wrapper. History warnings remain meaningful.

## Local checks

Runtime: `suifleet-sf164:protocol140`, image
`sha256:c6a6d33451dacf606473f9ce7060e66666e29207c939a37627872101f3ffcdd8`.
Pinned compiler 1.81.1, transaction CLI 1.83.0. Move/local-chain containers use
`--network none`, private empty compiler keystores and an owned copied Move cache;
the disposable chain creates only temporary test identities. No hardware runs.

| Exact command inside the container unless stated | Result |
|---|---|
| `make check-interfaces INSIDE_DEV_CONTAINER=1` | PASS compiled core/storage/data ABI, generated-file drift, ten negative ABI cases, shared BCS/recipe vectors and client catalog/control/rollout/receipt/inbox regressions |
| `sui move test --path contracts` | PASS 364/364 |
| `sui move test --path contracts/fleet_storage` | PASS 79/79 |
| `sui move test --path contracts/fleet_data --build-env testnet` | PASS 60/60 |
| `node scripts/test_security_policy.cjs` | PASS production policy parsing/builders and legacy/schema/type/network failures |
| `node scripts/test_security_delay.cjs` | PASS bounds, canonical SDK calls, React console countdown/cancel/early-disabled execution, authoritative queue BCS reads, missing timestamps, consumed actions, stale generations, tampering and transport errors |
| `node scripts/test_deployment_manifest.cjs` | PASS gRPC BCS origin/chain/package/registry proof and negative cases |
| `cd dapp && pnpm typecheck` | PASS |
| `cd dapp && NEXT_TELEMETRY_DISABLED=1 pnpm build` | PASS static production build |
| `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p test_deployment_manifest.py` (host) | PASS 23/23, including separate, complete and authenticated delay origins |

Core tests include exact timing boundaries, union and secondary classes, expiry,
current-admin versus outsider cancellation, protected reconfiguration/weakening,
mature recovery, all sixteen factor combinations and pause/resume separation.
The required bytecode/layout compatibility check uses an actual existing v197
customer, compatible upgrade and old-package calls on an isolated protocol140
chain, rather than a source scan or a mocked upgrade.

Final isolated command:

```sh
python3 scripts/test_security_policy_localnet.py --suite live_security_delay --base /base/contracts --evidence docs/validation/sf200-localnet.json
```

`/base/contracts` is exported from this branch's original merged-main HEAD.
The [local receipt](sf200-localnet.json) records the base/head bytecode hashes,
per-type origins, verified unsigned package transactions, 16 execution digests and
signed net gas. Conservative local allowance charged was 1,605,916,396 MIST;
net balance cost was 1,588,931,414 MIST. These disposable-chain costs are not public
network prices. All fixture state and keys were disposable.

## Public testnet

Dedicated agent account `0x0e499424b248e4cf18a5377a4e387487dbd299118b3b9d17d83fd1f716376965`,
chain `4c78adac`. Mutations held/asserted the global testnet lease; the worker
coordinated in #11 and released the lease before Review. No existing application
objects or SF-196 fixture was modified. The isolated bytecode-keyed package and
UpgradeCap were verified before reuse; no identical code was republished.

Initial execution used `scripts/live_security_delay.py` with the explicit agent
`client.yaml`, a private `sf200-state.json`, `--network testnet`,
`--max-spend-mist 1100000000` and `--evidence docs/validation/sf200-testnet.json`.
A preceding 1.6 SUI preflight requested one faucet attempt but its observed balance
remained below that ceiling. It submitted no acceptance transaction. The actual
run used the smaller 1.1 SUI allowance without another faucet request.

The first run stopped after six effects-checked transactions because the receipt
checker compared CLI base64 intent text to a byte array. This was a checker
representation error; the console's hash bytes were correct. The retained
[unsuccessful first receipt](sf200-testnet-attempt1.json) stays `success: false`.
The continuation reverified the original create/queue transaction effects,
customer/registry/nonce binding and current UpgradeCap, canceled its retained
nonce, and completed the remaining tests on that same organization. It used
`--resume-receipt docs/validation/sf200-testnet-attempt1.json` and a ceiling of
266,741,052 MIST: precisely the unspent original allowance. Credits and storage
refunds never increased either allowance.

The [successful continuation](sf200-testnet.json) proves direct refusal, immediate
cancellation, fresh queue, early Move abort, successful mature recovery and
ordinary all-off administration afterward. Cancellation consumes the nonce on
chain; attempting to rebuild execution with that deleted shared input fails
before signing. This is input refusal, not an invented failed execution digest.
The production console hash was independently compared to both on-chain Actions.
Each transaction has signed net gas plus its conservative ledger charge; negative
storage refunds remain negative net gas and charge zero to the allowance.
Read-only enrichment uses `scripts/record_security_delay_gas.py`; original
pre-enrichment receipt hashes preserve the continuation's provenance.

Total conservative allowance charged: **856,211,506 MIST (0.856211506 SUI)**.
Total signed net gas: **832,288,131 MIST (0.832288131 SUI)** across 14 transactions.
All setup/failure/recovery fees are included; no device funding was required.
The largest cost was package publication, 789,740,400 MIST. Every digest and gas
breakdown is in the two linked receipts.

## Corrected attempts and limits

Initial disposable runtime/configuration attempts failed before useful validation:
missing cache/config mounts, Git worktree metadata and mixed cache ownership.
A private owned cache and explicit empty compiler keystore fixed these. A first
customer setup budget of 20M MIST was insufficient on the disposable chain;
100M transaction reservations fixed it, without enlarging public allowance.
Two isolated upgrades caught the package limit (102,545 then 102,469 bytes versus
102,400). Removing redundant added API/serialization and combining the factor
and delay scan made the real final upgrade/publication pass. Nothing suppressed
the limit or relabeled these failures as success.

The core package is close to its hard size limit. Combined integration with other
mechanism tickets must pass actual publication/upgrade and preserve availability
bits by union; this source receipt is not evidence that an arbitrary combined
candidate fits. The installed coordinator owns independent review and exact
combined-head/base CI/testnet enforcement. Hosted CI is pending at handoff and
was not watched or polled. No merge or installed production rollout is claimed.

Human/outsider/extension-wallet, hardware-key/Pi, production-provider and actual
mainnet acceptance remains explicitly unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Production hosting remains [SF-042/#62](https://github.com/tyler-suifleet/suifleet/issues/62).
New documentation URLs receive publication checks after merge; premerge checks
verify committed paths/anchors. No keys, signing secrets or wallet credentials
are included in the evidence.
