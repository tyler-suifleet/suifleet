# SF-198 recovery evidence, 2026-11-01

Codex / gpt-6.1-sol; no finer runtime build exposed. Original claim, session
`dispatch-c81fa89e4e6a473cae8a970fd86dde45`, branch and worktree retained.
Draft [PR #451](https://github.com/tyler-suifleet/suifleet/pull/451) closes
[SF-198/#397](https://github.com/tyler-suifleet/suifleet/issues/397).

## Repair

Preserved unfinished source changes in 3b1d8ee2, then reconciled the quarantined
owner repair 44d677aab557 through merge 2e157009. The direct private-file read,
ENOENT-only generation and exclusive creation remain; malformed-file diagnostics
also conceal file content. Saved redirect-16 index/worktree/remote refs remain
unchanged. All commits use Tyler Bush as both author and committer.

Shared scoped authority setup and canonical BCS string literals reduce bytecode
without removing public functions, layouts or authority checks. Compiled root
modules total 93,469 raw bytes (16 modules); real package admission and compatible
upgrade now pass. Independent SDK BCS vectors check all 53 compact labels.

The protected CI security suite now calls native passkey acceptance using the
package gate's exact cached fixture and its existing 1.5-SUI ledger. It verifies
UpgradeCap owner/version/latest head and passkey type origin; it never republishes
packages. Keys and uncertainty markers use the existing encrypted CI fixture
state. No operator wallet, faucet or spending allowance was added. Fresh expensive
fixtures may exhaust the unchanged suite allowance; that is a failure, not a
waiver. Preserve caches and reconcile uncertainty rather than republishing.

## Verification actually run

Commands ran in `suifleet-dev:latest`, network none, host UID/GID, checkout at
`/workspace`; Move checks additionally mount the warm Move cache. Chain tests
mount the canonical Git metadata read-only, `/tmp/sf198-base/contracts` at
`/base/contracts`, the checksum-pinned 1.83.0 network CLI and built `chain_facts`.
The base is the exact pre-feature contracts from 02bca2dd.

- `sui move test --path contracts`: **359/359 pass**.
- `python3 scripts/generate_interfaces.py --check`: **16 modules pass**.
- `python3 scripts/check_move_security.py`: **pass**, strict linters,
  unsuppressed diagnostic checks, root359/storage79/data60 tests and actual
  failing authority mutations.
- `make test-customer-fleets` with `INSIDE_DEV_CONTAINER=1`: **pass**, SDK signing,
  tamper/binding/recovery/browser/transport negatives, private-file subprocess
  regressions, policy/customer suites, real Chromium console regressions and
  wallet bundle smoke check. This is automated software evidence, not a manual
  extension-wallet/hardware-authenticator walkthrough.
- `cd dapp && pnpm typecheck && pnpm build`: **pass**, static production export.
- `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p
  'test_testnet*.py'`: **175 tests pass, one skip**.
- `PYTHONPATH=scripts/tests:scripts python3 -m unittest test_live_passkey
  test_testnet test_live_common`: **115 pass**. An initial invocation named the
  nonexistent `test_testnet_live_common` module; corrected to `test_live_common`.
- Python syntax compilation, Node syntax check and `git diff --check`: **pass**.
- `python3 scripts/test_security_policy_localnet.py --runner
  scripts/live_passkey_factor.py --base /base/contracts --evidence
  docs/validation/sf198-recovery-localnet.json`: **pass, 35 effects-checked
  transactions**. The receipt contains package IDs, split type origins, all
  digests and per-transaction net gas. Both standalone and cached CI adapter
  cases ran against the actual disposable node: possession/enrollment, policy,
  allowed wallet+passkey action, refused wallet-only and old-package calls,
  backup-key recovery, removed-key refusal, surviving-key action and disable.
  Allowance used **1,730,358,233 / 1,800,000,000 MIST**, including grants.

The first repeated cached-adapter attempt failed before submission because a
reused passkey coin fell below the 20M full reservation. Raising its top-up
threshold from 15M to 30M fixed it without rotating identities or increasing the
allowance. Final receipt reports starting Git head 893042ec with that one-line
runner correction present; the correction is committed in 5fa041eb. Contract and
frontend sources match those tested; later changes only record this evidence.
Historical failed receipts remain historical and are superseded only for the
specific local checks now passed.

## Outstanding gates

Explicit read-only agent-wallet `active-address` still returns `None`. No public
wallet was created or rotated and no public mutation or faucet call was attempted.
The automatic protected lane is wired for native feature validation, but its
public-testnet effects are **not yet observed**. Push requests fresh exact-head
ordinary CI/CodeQL through the PR event; hosted results remain pending and are
not polled. Local CodeQL CLI is unavailable; scanner remediation is not claimed
verified by CodeQL. Keep PR draft and claim reserved pending genuine public
feature evidence and remaining review/security gates. No Review/approval/merge
is claimed. No shared resources remain held.

Manual real-wallet/hardware-key, physical, provider and mainnet acceptance remain
unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159). Their deferral does
not waive software/security/public-testnet gates.
