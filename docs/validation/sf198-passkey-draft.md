# SF-198 blocked draft evidence

Codex, configured/running model gpt-6.1-sol; no finer model build is exposed.
Session `dispatch-c81fa89e4e6a473cae8a970fd86dde45`, assigned branch
`work/suifleet/sf-198-6874ced3`. SF-196 is merged. No other ticket claimed,
no workers spawned and no public mutation, hardware programming or deployment.

The draft uses a native Sui passkey as transaction gas sponsor and the current
administrator wallet as sender. Both sign identical bytes; Move authenticates
sponsor address against possession-proven enrolled passkeys. Enrollment/removal,
all-off defaults, policy activation, browser metadata backup/restore, selection
of a surviving recovery key and wallet signing are implemented as a draft.
Native signatures have **not** yet been accepted by a real node for this feature.

## Actual checks

Commands ran in `suifleet-dev:latest`, image
`sha256:c8b9afa8c874d5c49b65570ffff44596f2501dc7a22fbc0532d612e6a34f4c0d`,
using Node 22.22.2 / pnpm 11.0.8 and release compiler 1.81.1. The old image's
network CLI is stale; chain containers mounted checksum-verified Sui 1.83.0
`29e1beb96434d0d2e00642929dbab1d8813039b80c3eb514bbae2751fb2648fa`.
Read helper `chain_facts` was reused read-only from the SF-196 build; it was not
rebuilt here. Host read-only wallet checks used Node 24.21.0 only for preflight;
no transaction was built or signed on that path.

- `sui move test --path contracts`: PASS 359/359, including nine new passkey tests.
- `python3 scripts/check_move_security.py`: PASS strict security/linter and
  authority mutation checks, storage 79 and fleet-data 60 tests.
- `python3 scripts/generate_interfaces.py --capture`: PASS 16 compiled modules;
  generated TypeScript/Rust and introduction-origin definitions synchronized.
- `node scripts/test_passkey_factor.cjs`: PASS real SDK signature/tamper checking,
  production metadata recovery, transaction builders, binding, browser/transport
  negatives. Node crypto synthesizes WebAuthn; no real browser authenticator used.
- `node scripts/test_security_policy.cjs`: PASS defaults, masks, wire parsing,
  policy builders and legacy/type/schema/network negatives.
- `node scripts/test_customer_fleets.cjs`: PASS existing discovery/authority tests.
- `pnpm typecheck` and `pnpm build` in dapp: PASS; static production export built.
- `python3 -m unittest discover -s scripts/tests -p test_deployment_manifest.py`:
  PASS 22 tests.
- Initial `make INSIDE_DEV_CONTAINER=1 dapp-typecheck`: FAIL, 175 offline testnet
  tests with six environment errors (five missing PyYAML, one stale image CLI).
  Corrected rerun mounts existing PyYAML and the verified transaction CLI:
  PASS exit 0, including 175 offline testnet, 67 shared-live, 22 manifest tests
  and final frontend typecheck. No failure was suppressed.
- Final `python3 scripts/generate_interfaces.py --check`: PASS 16 modules.
- Final `pnpm build`: PASS static export after the wallet funding guard change.
- `git diff --check` and Python syntax compilation: PASS.

## Real isolated-chain failures

`python3 scripts/test_security_policy_localnet.py --runner
scripts/live_passkey_factor.py --base /base/contracts --evidence ...` ran with
Docker `--network none`, temporary wallet/node/faucet and private signing state.
The base is the exact pre-change contracts exported from assigned base `02bca2dd`.
No shared port or testnet resource was held.

1. `sf198-localnet-attempt1.json`: base publication succeeded, customer creation
   failed `InsufficientGas` at a 20,000,000 MIST budget; 760,125,759 local MIST used.
   Runner creation reservation was raised to 100,000,000 MIST.
2. `sf198-localnet-attempt2-size.json`: base/customer/migration succeeded, upgrade
   failed `MovePackageTooBig`: 103,636 > 102,400 bytes.
3. `sf198-localnet.json`: after removing redundant draft secured wrappers, upgrade
   still failed `MovePackageTooBig`: 103,244 > 102,400 bytes. This is an unresolved
   **software deployment defect**, not deferred external acceptance. The feature
   transactions after upgrade never ran. No chain compatibility pass is claimed.

Earlier harness setup attempts also failed before feature transactions: missing
faucet readiness, missing chain_facts mount and missing linked-worktree Git mount.
The local wrapper now waits for faucet readiness and accepts an explicit runner;
all temporary node subprocesses were stopped by its finally cleanup.

## Public wallet blocker and next work

Read-only commands with explicit dedicated config:

```
/tmp/sf196-network-183/sui client --client.config /home/tyler/.config/suifleet/testnet/agent/client.yaml active-address
/tmp/sf196-network-183/sui client --client.config /home/tyler/.config/suifleet/testnet/agent/client.yaml addresses --json
```

Actual results: `None` (exit 0); `No managed addresses. Create new address with
new-address command.` (exit 1). No keys were created, changed or exposed. The
public acceptance run was not started, no faucet requested and no testnet lock
was acquired. Do not create another wallet outside the dedicated account grant.

Next owner must reduce or restructure the root package without weakening
factor/authority checks; rerun real existing-object upgrade and native signature,
refusal, old-package and surviving-key recovery tests. Restore access to the
existing dedicated wallet (or use the protected CI lane), implement reviewed
cached-base fixture reuse for public runs rather than republishing identical
base bytecode, then perform locked/bounded testnet setup and feature effects.
Finish actual console/browser flow acceptance and independent source review.
The branch is a blocked draft, **not Review-ready**, and hosted CI is not approval.

Manual real-wallet/hardware-key, Pi, provider and mainnet checks remain unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
Those deferrals do not waive the package-size, local native execution or automatic
testnet gates. Keep the existing implementation claim reserved and Blocked.
