# SF-201 multisig administrator validation

Implementation owner: Codex, configured model **gpt-6.1-sol** (runtime identifies
GPT-6; a finer serving build/version is not exposed), session
`dispatch-0930bac7f3744e72a26b38f892875189`, branch
`work/suifleet/sf-201-fa7db1a2`. Depends on merged SF-196/#384, base
`02bca2dd`. Existing implementation issue: [#400](https://github.com/tyler-suifleet/suifleet/issues/400).

Native Sui authentication proves the enrolled multisig sender. The contract
validates bounded flagged public keys, weights, threshold and derived address,
requires existing administrator authority, and stores their authenticated binding.
Approval checks configuration, current credential, organization, generation and
expiry; existing wrappers retain exact intent, proposer and single-use checks.
Only bit 1 becomes available; other unavailable factors remain fail closed.
All-off defaults, custody seals and current-factor weakening/recovery rules remain.
The console supports exact-byte export/import, member wallet signing, imported
hardware/tool signatures, local verification, quorum assembly and effects checks.
Gas and full decoded commands are visible; bundle labels are untrusted metadata.

## Commands and actual results (2026-11-01)

- `python3 scripts/check_move_security.py`: passed the pinned compiler, strict
  extra/unsuppressed lint baseline and adversarial mutation regressions (20 PASS
  summaries). Raw logs remain private.
- `sui move test --path contracts`: **360 passed**, including ten new multisig
  cases (independent SDK address vector, configuration bounds/duplicates, ordinary
  administrator refusal, substituted configuration/action and exact recovery).
- `sui move test --path contracts/fleet_storage`: **79 passed**.
- `sui move test --path contracts/fleet_data --build-env testnet`: **60 passed**.
- `python3 scripts/generate_interfaces.py --check`,
  `python3 scripts/check_contract_interfaces.py`: generated ABI/origin consistency
  and **1,268 references, zero drift**. Nested byte vectors/u16 now have real
  generated transaction encoders; unsupported types still fail closed.
- `python3 scripts/test_generated_interfaces.py`: ABI mutation/drift regression
  checks (including the updated unsupported-type set after adding u16 support).
- `node scripts/test_protocol.cjs`: 60 shared recipe cases plus independent
  BCS event/key vector and SDK call encoding passed.
- `node scripts/test_multisig_admin.cjs`: real SDK address/key-scheme/configuration,
  sender/deployment, wrong member, duplicate, changed bytes, insufficient quorum,
  alternate surviving quorum, execution failure, uncertain submission and confirmed
  effects checks. Transport failure requires digest reconciliation.
- `node scripts/test_multisig_admin_browser.cjs`: actual Chromium component,
  authenticated-key form/address/threshold display, wrong-member refusal before
  signing, changed wallet bytes refused, exact member signing and duplicate partial
  refusal. Browser transport/wallet fixtures are explicitly simulated.
- `node scripts/test_security_policy.cjs`: all-off settings, available/unavailable
  masks, schema/type/version/network rejection and real SDK policy builders.
- `pnpm --dir dapp typecheck`, `pnpm --dir dapp build`: passed using locked
  dependencies installed with pnpm 11.0.8. Next's existing module-type warning
  remains nonfatal. No dependency or lockfile upgrades.

Move/tooling ran in `suifleet-sf164:protocol140`, with the pinned release compiler
1.81.1 and checksum-verified transaction CLI 1.83.0. Isolated chains used Docker
`--network none`, temporary wallets/nodes and the warmed host Move cache. A
read-only prebuilt SF-196 review `chain_facts` reader was mounted at
`/tools/chain_facts`; SHA-256
`8a0cc8beb7fd34e1c4cc20c42c32c02c1b62abf2b2ce778fdf189d810c126394`.
Final compiled contract digest equals the actual public fixture identity.
That reader is unchanged by this ticket; no new Rust runtime behavior is claimed.

### Isolated upgrade and native authentication

Export base with `git archive 4fb5900e contracts` into `/base/contracts`, then run
inside the disposable network-none container (mount the checkout's Git metadata
read-only, Move cache, and the reader above):

```sh
python3 scripts/test_security_policy_localnet.py --base /base/contracts \
  --evidence docs/validation/sf201-upgrade-localnet.json \
  --multisig docs/validation/sf201-native-localnet.json
```

The [existing-object upgrade receipt](sf201-upgrade-localnet.json) records **16
transactions**, actual original-base to head upgrade, old-package credential,
fleet and capability-loan refusals, unavailable-factor refusal, publisher custody
exclusion and successful current publication/withdrawal. Allowance accounting:
1,429,369,147 local MIST, including budget funding; actual summed transaction net
gas 1,428,369,147 MIST. This is disposable local gas, not public-network spending.

The [native quorum receipt](sf201-native-localnet.json) records **11 transactions**,
2-of-3 registration/enrollment/activation, ordinary administrator downgrade and
forged-approval failures, exact quorum recovery, then restored ordinary authority.
Allowance 888,344,473 local MIST including 80,000,000 MIST quorum funding; summed
net gas across both identities 818,892,877 MIST. The local descriptor
network label inherited from the testnet-key utility was corrected to the verified
local chain (keys, addresses, effects and gas unchanged); the harness now records
the actual network directly. The native-only rerun command adds `--only-multisig`; the failed initial 20m organization-creation gas reservation
was raised to 35m before this successful native rerun. The initial missing reader
and Git-metadata mounts also failed before usable chain acceptance. These setup
failures are not reported as passing tests.

### Authorized public testnet

Under a short-lived `network:suifleet-testnet` lock, dedicated agent client config
`/home/tyler/.config/suifleet/testnet/agent/client.yaml` and strict allowance:

```sh
python3 scripts/live_multisig_admin.py \
  --wallet /home/tyler/.config/suifleet/testnet/agent/client.yaml --network testnet \
  --state /private --evidence docs/validation/sf201-native-testnet.json \
  --max-spend-mist 1050000000 --auto-fund
```

The [actual public receipt](sf201-native-testnet.json) verifies chain **4c78adac**,
dedicated agent **0x0e499424…6965**, fixture UpgradeCap ownership and package/type
origins before organization writes. It records **11 actual transactions**, every
digest, execution status and net gas. A separate read-only CLI pass re-read all
11 statuses and net gas values (`independent_readback`). The lease was released
immediately afterward; coordination and spend summary are recorded in
[SF-010/#11](https://github.com/tyler-suifleet/suifleet/issues/11#issuecomment-6565728642).

- Non-replenishing parent allowance used: **901,156,496 / 1,050,000,000 MIST**,
  including **80,000,000 MIST** funding to the run-owned quorum; its operations
  have their own bounded child ledger within that grant. Summed net transaction
  gas across both identities: **831,704,900 MIST**. Funding is not a network fee.
- One authorized faucet request; neither faucet credits nor rebates replenished
  the allowance. No default wallet or original app objects were selected.
- Allowed quorum recovery:
  `8evnhtwoj3XKxXuYNMSajdnqwpqdWyKrTnHMQR6QMPty`.
- Refused ordinary-administrator approval:
  `6vHqfdPTyg33qgJ4gb7i1DdKcrRAgMbY7gzEg6g8DJ6X`.
- Native fixture publications are bytecode-keyed and reserved on uncertainty;
  run state prevents replaying started/completed setup/funding. Private keys and
  state are outside the checkout, never in evidence.

## Remaining acceptance and review

Manual Slush, extension-wallet and actual hardware/passkey-member sessions remain
**unrun**, retained in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32).
The implemented scheme support, wallet-standard entry point and signature import
are software evidence, not proof of each physical wallet/browser integration.
Production-provider/real-account and actual mainnet acceptance remain unrun in
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159), hosting in
[SF-042/#62](https://github.com/tyler-suifleet/suifleet/issues/62). No physical
programming, production credentials or mainnet operations occurred.

Exact pushed-head hosted CI, automatic testnet CI and independent source review
are **pending** at handoff, enforced by the coordinator. No CI polling or merge
was performed. This evidence establishes implementation readiness for Review,
not final integration acceptance or production readiness.
