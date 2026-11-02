# SF-200 pinned SF-201 composition repair

Source repair: `ff1ec33da3924308f2ea761a6ced6e515b76ab5c`, combining reviewed source
`8f3553f061d0779284eb927e6a57d1e1aa12e573` with the explicitly pinned conflicting
candidate base `df5d6d8005801034a75d4ce4180dcf713d82efd8`. This merge resolves
an actual conflict; it is not a freshness merge or a merge to main.

Both factor bits (1 and 8), console panels, test entry points and operational
procedures are retained. The multisig `prepare_policy_change` helper now encodes
Customer identity consistently with the delayed executor. The Move regression
calls that production helper; before repair it failed exact-intent verification.

## Checks run

In `suifleet-sf164:protocol140`, network disabled, Sui 1.81.1, cached `sf200-move`
dependencies, temporary empty compiler keystore:

- `sui move test --path contracts`: PASS 376/376 (rerun after final test change).
- `sui move test --path contracts/fleet_storage`: PASS 82/82.
- `make check-interfaces INSIDE_DEV_CONTAINER=1`: PASS compiled ABI, generated
  drift, negative ABI checks, protocol vectors and client regressions.
- `node scripts/test_security_policy.cjs`: PASS, including availability masks
  1, 8 and 9 and refusal of unsupported factors.
- `node scripts/test_security_delay.cjs`: PASS canonical arguments, queue readers,
  countdown/cancel/execution rendering and tamper/failure cases.
- `node scripts/test_multisig_admin.cjs`: PASS native vectors and quorum negatives.
- `CHROME_EXECUTABLE=/opt/chrome-headless-shell/chrome-headless-shell node scripts/test_multisig_admin_browser.cjs`:
  PASS actual Chromium component with fixture wallet/transport.
- `cd dapp && pnpm typecheck && NEXT_TELEMETRY_DISABLED=1 pnpm build`: PASS.
- `python -m py_compile scripts/test_security_policy_localnet.py`: PASS.
- `git diff --check` and staged equivalent: PASS.

Initial environment attempts failed: host pnpm unavailable; nonroot build could
not unlink existing root-owned Next output; incorrect temporary HOME/cache mounts;
missing chain-facts executable; an older helper lacked protocol-version output;
and the first correct helper run lacked the linked Git metadata mount. These were
setup failures, not passes. The successful frontend run used the container's root
user for existing build output. Interface/Move runs used uid 1001 and an empty
keystore. No production or public-testnet wallet was mounted.

## Blocking combined package-size finding

`python3 scripts/test_security_policy_localnet.py --suite live_security_delay --base /base/contracts --evidence /workspace/docs/validation/sf200-multisig-combined-localnet.json`
ran in the same network-none container, with the pre-framework source exported
from `02bca2dd8efcc2a0c4b72756a8a38eb725e55d0e`, read-only Git metadata and a
read-only `chain_facts` helper from the SF-089 worktree (its Rust example source
matches this tree). It **FAILED** the actual compatible upgrade with
`MovePackageTooBig { object_size: 104200, max_object_size: 102400 }`.

The [failure receipt](sf200-multisig-combined-localnet.json) preserves all three
transaction effects/digests and verified unsigned package hashes. Disposable chain:
`05b7916e`; conservative allowance used: 790307621 MIST of 3000000000 MIST.
The receipt names the precommit source HEAD because compilation ran before the
repair commit; its compiled functional source is exactly the repair above.
This is a real combined-candidate source acceptance failure, independent of the
historical protected-CI funding shortage. Do not retry publication on public
testnet until the combined bytecode fits. Reduce package bytes compatibly, rerun
unit/console/interface checks and this isolated upgrade, then obtain actual
exact-head/base CI and automatic public-testnet effects/cost evidence.

No hosted CI was watched/polled. Public testnet was not mutated. No merge,
deployment or hardware operation occurred. Manual wallet/Pi/provider/mainnet
acceptance remains unrun in SF-031/#32 and SF-087/#159; hosting in SF-042/#62.
This receipt is not approval and does not mark acceptance complete.
