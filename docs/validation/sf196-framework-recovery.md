# SF-196 recovery evidence

Codex, configured running model `gpt-6.1-sol`; no finer runtime version is exposed.
Existing session/branch: `dispatch-487779ccfe9444d1b0ba30c4fbf895d0`,
`work/suifleet/sf-196-267c81b9`. This records a partial implementation, not Review.
The user approved all four optional protections in #384 on 2026-10-31; no human
review hold remains. No existing dirty or staged work was present at recovery.

All checks used `suifleet-dev:latest`, image
`sha256:c8b9afa8c874d5c49b65570ffff44596f2501dc7a22fbc0532d612e6a34f4c0d`.
Offline Move containers used `--network none`, a private copy of the host Move
cache and a keyless testnet compiler configuration. Frontend install used the
committed frozen lockfile. No live wallet was mounted into offline test containers.

Executed commands (inside that image, checkout at `/workspace`):

- `pnpm install --frozen-lockfile` in `dapp`: PASS.
- `sui move test --path contracts security_policy`: PASS, 10 tests, including all
  16 requirement masks in the test-only kernel, missing factors, changed amount,
  stale generation, cleared grants, weakening, cancellation, unauthorized proposal,
  unavailable activation and default/current credential behavior.
- `sui move test --path contracts`: PASS, 300/300, repeated after new-organization
  default-policy creation. Existing deprecated `vec_map::size` warnings remain.
- `python3 scripts/generate_interfaces.py --capture`: PASS, 15 modules. The
  captured ABI was separately compared to HEAD: every existing public signature,
  ordered layout and ability is unchanged. Only additive interfaces are accepted.
- `python3 scripts/generate_interfaces.py --check`: PASS, 15 compiled modules
  match the reviewed/generated interfaces.
- `node scripts/test_security_policy.cjs`: PASS; actual production parser/SDK
  builders, 16 masks, unavailable/invalid settings, legacy version, foreign type,
  unsupported schema and chain mismatch. Fixtures do not establish live factors.
- `node scripts/test_customer_fleets.cjs`: PASS, existing customer discovery,
  revocation/isolation, capability loans, read-only Seal and reader/support cases.
- `pnpm typecheck` in `dapp`: PASS after fixing an initial nullable query-data error.
- `pnpm build` in `dapp`: PASS, static production routes including `/settings`.
- `git diff --check`: PASS before staging (repeat for final commit).

Initial Move attempts exposed local invocation/cache/compiler errors (unsupported
`--filter`, missing offline cache/configuration, missing BCS import, mutable borrow,
restricted test key construction and generated `mut` argument syntax). They were
repaired before the successful runs above; they are not acceptance evidence.

Read-only dedicated-agent testnet preflight used `Live.preflight(auto_fund=False)`,
checksum-verified compiler/transaction CLIs, wallet `/w/agent/client.yaml` mounted
read-only from the dedicated agent configuration, and the existing SF-010
`chain_facts` reader mounted read-only. The initial mount missed the keystore's
configured `/w/agent` path and failed account parsing; corrected mount reached the
normal compatibility gate. Result: **REFUSED**, network protocol 140 exceeds the
pinned transaction CLI's supported 139. No signing, faucet, publication, upgrade,
object mutation or spending occurred; no mutation lock was acquired or needed.
This is a preflight refusal, not a completed public-testnet feature test.

Missing acceptance remains in SF-196/#384: external-capability scoping,
catalog/withdrawal/handover coverage, domain/revision binding, per-class and pause
semantics, enrollment/edit workflows, isolated-chain old-package compatibility,
security checks and actual public-testnet feature effects/digests. The source
patch has not activated any production mechanism. See SECOND_FACTOR_DESIGN.md.
Physical/mainnet/manual-human/provider acceptance remains unrun in #32/#159.
Hosted CI has not been polled or treated as approval.
