# SF-140 Data UI implementation evidence

Owner: Codex, configured/running model `gpt-6.1-sol` (no finer build identifier
available). Branch `work/suifleet/sf-140-303dfa87`; assigned worktree and session
are recorded in SF-140/#285. This is implementation evidence, not production or
manual-wallet acceptance.

## Scope and prerequisite

User-authorized development prerequisite SF139 PR302 was integrated at exact
`b696a61bdbd0769cad1c2b7f86e19bbe73330e13`, preserving upstream history in merge
`2ab6f3d`. Its contract/producer/crypto helper work remains SF139-owned; SF140 changes
only UI, UI data/pagination utilities, tests and documentation/tracking. UI-only
ownership and the immutable helper boundary were coordinated in #284/#285.
SF139 must merge before SF140 integration. Native issue dependencies block all
implementation assertions, so the merge dependency is restored at Review handoff,
after the explicitly authorized pinned-prerequisite development is complete.

The Data section adds bounded generic-item discovery and filters, device/group/
stream details, scoped owner/reader sessions, verified chunk/complete-upload
downloads, small escaped UTF-8/JSON previews, scoped owner grants/revocations and
direct customer HTTPS export. Existing per-device logs/diagnostics/shadow browsing
is preserved. See [dashboard behavior and limits](../DASHBOARD_VIEWS.md#device-data-browser-sf-140).

No production crypto was handwritten: reads use `readDataItem`, `fetchDataItem`
and `scopedDecryptor`; complete uploads pass original verified buffers into
`assembleDataItems`. Grants use `setReadGrant` and the existing effects-checked
wallet hook. Cancellation is checked between chunks and before additional wallet
signing; late buffers are wiped. No helper/contract API changes were needed.

## Verification on the implementation worktree

Toolchain: Node `v22.22.2`, pnpm `11.0.8`, Google Chrome `153.0.8010.36` on the WSL
host. Locked dependencies were installed with `pnpm@11.0.8 install --frozen-lockfile
--dir dapp`. Host commands used the pinned Node/pnpm on PATH; `RUN=` selects host
execution without claiming to be inside the dev image.

- `make dapp-typecheck RUN=`: **passed**, including offline testnet/manifest,
  existing wallet/query/authority/receipt/Seal/archive regressions, the new Data
  unit/browser tests, and `tsc --noEmit`. The 80-test offline testnet suite reports
  one intended host skip: installed-container toolchain verification. Other
  Python groups reported 36, 17, 6 and 3 tests passed. No live testnet run is
  implied by these offline tests.
- `pnpm --dir dapp typecheck`: **passed again** after final cancellation guards.
- `node scripts/test_data_browser.cjs`: **passed again** after final changes.
  Covers bounded one-page-per-action discovery, cursor refusal, device rotation
  and unavailable-device progress; all filters/time errors; preview bounds;
  real signed scoped ciphertext and independent AES-GCM opening; ciphertext
  tamper/access denial; verified assembly/copy refusal; actual scoped grant PTB
  construction; HTTPS/credential/redirect/export errors.
- `node scripts/test_data_browser_browser.cjs`: **passed again** after final
  changes using host Chrome. Exercises the real Data component, filters/details,
  safe JSON preview, actual downloaded bytes, binary refusal of preview, complete
  upload, owner/reader controls, grant/revoke PTBs, multi-item unique export paths,
  destination 403 failure, digest/key denial, scope reset during an in-flight read,
  wiped retained buffers and empty web storage. Playwright fulfills synthetic
  HTTPS requests; no application/fixture server or listening port is started.
- `node scripts/test_fleet_bulk.cjs`: **passed**, preserving existing signed archive
  and live-availability refusal behavior with explicit offline chain fixtures.
- `make dapp-build RUN=`: **passed again** after final cancellation guards; all
  15 static pages generated. Existing Tailwind module-type warning is nonfatal.
- `make test-tracking RUN=`: **10 tests passed**.
- `make tracking-plan RUN=`: **passed**, 119 prepared issues, dry run with no GitHub
  reads/writes, authentication or state mutation.
- `git diff --check`: **passed** before staging.

The first full make attempt mistakenly supplied `INSIDE_DEV_CONTAINER=1` on the
host and failed the installed-toolchain check because `sui` was absent from host
PATH. It was corrected by using the actual host mode (`RUN=`), then the complete
make target passed. No application test was ignored or suppressed. Initial
browser fixture failures (missing fixture process shim, select accessible labels,
and an assertion before React scope cleanup committed) were repaired and rerun.

## Limits and remaining gates

Metadata, sizes and unsalted plaintext hashes remain public; predictable content
can be guessed offline. Legacy archives retain their fleet-wide policy. Revocation
cannot recall plaintext or already obtained keys; epoch rotation denies older
keys including owner reads. The visit holds at most 256 devices/512 generic chunks;
filters cover loaded metadata only. Complete uploads require all chunks loaded;
exports are bounded sets of chunks, not an atomic customer-storage transaction.
URLs/tokens and plaintext stay in tab memory and are discarded on scope changes;
JavaScript/browser copies are not guaranteed erased from memory.

Provider authorization, fullnode/index responses and transaction execution in the
new UI fixtures are explicit stand-ins. Signatures, scoped encryption, integrity
verification, assembly and grant transaction construction are real pinned SDK/
application code. No new live provider, real-wallet, chain or hardware result is
claimed. Historical SF139 evidence is not a current SF140 test run.

At push, hosted ordinary/security CI and automatic public-testnet validation are
**pending**, not approved or waived. The coordinator must enforce actual exact-head/
current-base CI/testnet, fresh independent review and upstream SF139 merged-main
before integration. No CI polling/watchers or model workers were started.
Final manual wallet/IdP/provider, physical target and actual-mainnet acceptance
remains unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159). No mainnet, hardware,
production credentials or machine-wallet mutation occurred. No migration is added
by this UI; its deployment must use the updated SF139 package and verified origins.
