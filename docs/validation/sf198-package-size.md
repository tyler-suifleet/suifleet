# SF-198 package-size CI repair (2026-11-03)

Automatic testnet [run 41704112541](https://github.com/tyler-suifleet/suifleet/actions/runs/41704112541)
failed at the compatible upgrade, with effects `MovePackageTooBig`:
103,941 bytes versus 102,400. Its public effects artifact confirms chain
`4c78adac`, failed digest
`3cknyAoUeKLEadqDcGpRWxw8cPCU7NmL2P7HEcqWhUth`, and 7,580,163 MIST charged
for that failure. Total recorded spending/allowance was 795,393,919 MIST.
These are historical CI effects, not a new machine-session transaction.
The existing encrypted state, funded identities, reservations and spend limits
were not reset or changed.

The repair shares repeated same-error guards, retaining left-to-right
short-circuit refusal before indexing or arithmetic; widens narrow version,
limit and index constants at their u64 use sites; shortens private symbols;
and removes the passkey module's two single-use forwarding wrappers. All public
signatures, layouts, type origins, values and abort codes are preserved.
The factor refusal in `security_policy::check` retains its original abort site,
so the independent live refusal checker remains unchanged. No linter baseline,
security suppression, budget, spending ceiling or retry policy was changed.

`scripts/check_package_size.py`, now part of `make check-interfaces`, compiles
release bytecode with the same pinned toolchain helper as the live harness and
accounts for module bytes/names, datatype origins, dependency linkage and the
version using Sui's `MovePackage::size` formula. Against an archive of original
head `c1fa03ceb81f8196f147345a046a1f6d24f34eeb`, it fails with exactly
103,941 bytes. The repaired package measures **102,332 / 102,400 bytes**,
a reduction of 1,609 bytes. Only **68 bytes** remain: further root-package
changes must pass this gate and may need additional compaction. The network
remains authoritative; the offline check does not assert chain acceptance.

## Local checks

Commands ran in network-none Docker `suifleet-sf221:localtest`, with
`/tmp/sf198-private-move` mounted at `/home/dev/.move`, the checkout at
`/workspace`, and `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory`,
`GIT_CONFIG_VALUE_0=*`. Make targets used `INSIDE_DEV_CONTAINER=1`.
The compiler is pinned Sui 1.81.1; the disposable-chain transaction CLI is the
checksum-pinned Sui 1.83.0. No public wallet was mounted.

- `make test-contracts`: passed 443 root, 82 storage and 60 data tests before
  adding the two new short-circuit regressions.
- `sui move test --path contracts`: passed 445 tests with those regressions,
  including empty-key refusal before indexing and past-expiry refusal before
  subtraction. The final compiler/security gate reruns the full suites.
- `make check-interfaces`: passed the size check, two size-accounting/boundary
  unit tests, compiled ABI/layout and generated-interface checks and frontend
  protocol/recipe/catalog/deployment/rollout/receipt/inbox regressions.
- `python3 scripts/check_move_security.py`: passed the pinned compiler and
  strict linters, unchanged unsuppressed compatibility baseline, all 445 root /
  82 storage / 60 data tests, and genuine failing authority-bypass mutations.
- Host `node scripts/test_passkey_factor.cjs`,
  `node scripts/test_passkey_payment.cjs`,
  `node scripts/test_security_policy.cjs`, and
  `node scripts/test_security_delay.cjs`: passed.
- `python3 -m unittest discover -s scripts/tests -p test_check_package_size.py`:
  two tests passed, including the observed oversized package and exact boundary.

## Disposable-chain effects

`python3 scripts/test_security_policy_localnet.py --runner
scripts/live_passkey_factor.py --base /base/contracts --evidence
docs/validation/sf198-size-localnet.json` passed, using an archive of the exact
pinned PR base `0341a1ef1f79c96c36015c7486faf486acaee6f2` at `/base`.
The local-chain setup used the additional transaction CLI, chain_facts and PyYAML
mounts documented in [the earlier run](sf198-recovery.md). The global localnet
build lease was asserted/renewed while in use and released after node exit.

The [sanitized receipt](sf198-size-localnet.json) records **23 effects-checked
transactions**, compatible existing-object upgrade, native possession/enrollment,
allowed action, wallet-only/old-package/removed-key refusals, backup-key recovery,
disable and final removal. Net spend was **1,653,109,047 MIST**; conservative
allowance including grants was **1,721,236,825 / 1,800,000,000 MIST**.
Both interrupted enrolled keys were reused. Every digest, type origin and fee
is in the receipt. This is an isolated disposable chain, not public testnet.
Its `head` identifies the pre-commit checkout; compiled transaction bytes/digests
came from this repair's working tree, subsequently committed with this evidence.

Initial local attempts are not passed evidence: the first build lacked a safe
Git cache configuration; the initial constant rewrite was rejected by Move's
u64 test-attribute requirement and named-abort linter, and was discarded.
Early compact candidates still exceeded the size limit. A subsequent local
upgrade succeeded but the unchanged refusal checker caught a moved abort site;
the repair restores that site instead of broadening the checker.

Public automatic exact-head/current-base testnet and ordinary/security CI,
and fresh independent review, remain required after push. No hosted CI is
polled or claimed successful here. Manual wallet/authenticator, physical Pi,
production-provider and actual-mainnet acceptance remains unrun in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
