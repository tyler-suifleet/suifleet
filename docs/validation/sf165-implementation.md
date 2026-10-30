# SF-165 implementation evidence

Codex running model/version: **gpt-6.1-sol**. Session:
`dispatch-371f0829aac544ffaa8408af51f62087`; branch
`work/suifleet/sf-165-bfc626f9`. Existing issue: [#331](https://github.com/tyler-suifleet/suifleet/issues/331).
No contract layout, interface schema, dependency or migration changed.

## Local verification (2026-10-27)

The pinned `suifleet-dev:latest` image reports Node 22.22.2 and pnpm 11.0.8.
The worktree and existing pnpm content store were mounted at their identical host
paths so pnpm's dependency verification did not attempt an unnecessary reinstall.
Container networking was disabled for the application build/offline suite.

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD:$PWD" -w "$PWD" \
  -v /home/tyler/.local/share/pnpm/store:/home/tyler/.local/share/pnpm/store:ro \
  -e INSIDE_DEV_CONTAINER=1 suifleet-dev:latest make dapp-typecheck

docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD:$PWD" -w "$PWD" \
  -v /home/tyler/.local/share/pnpm/store:/home/tyler/.local/share/pnpm/store:ro \
  -e INSIDE_DEV_CONTAINER=1 suifleet-dev:latest \
  bash -c 'cd dapp && pnpm typecheck && cd .. && make dapp-build'

node scripts/test_release_storage.cjs
node scripts/test_seal_frontend.cjs
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s scripts/tests -p test_live_release_storage.py
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_components_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_upload_browser.cjs
CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_wallet_browser.cjs
```

All commands passed. `make dapp-typecheck` includes its offline testnet/controller,
manifest, signing, privacy/storage, data/browser and enrollment regressions; the
new signing-policy unittest reports 3 passing tests. The final production build
exports all pages successfully. The Components browser test changes the period
to 12 days and verifies every upload request receives 12 epochs. Upload browser
coverage includes explicit file/price review before upload, real progress events,
duplicate clicks, failed uploads, cancellation/retry and confirmed success. The
Wallet Standard fixture covers real provider/connect and application transaction
hooks with fixture effects, including rejection, failed effects and wrong chains.
Seal inspection tests use real encrypted envelopes for namespace/provider/AAD and
expiry negatives, an isolated key-server response for successful policy inspection,
digest verification, plaintext-byte wiping and bounded downloads. Catalog binding
tests cover ordinary/sharded introduction origins and foreign-value rejection.

Earlier attempts identified and repaired the probe's expanded return-value
expectation and the wallet harness's missing `process.env` browser shim. A first
container mount at `/workspace` mismatched the host-installed pnpm store; using
identical paths resolved that environment failure. Initial host dependency setup
used Node 24.21.0/pnpm 12.14.0, followed by frozen/offline pnpm 11.0.8 verification;
final application typecheck/build used the pinned image. No lockfile changed.
The build emits its existing Tailwind configuration module-type warning.

## Dedicated public Testnet

[The sanitized receipt](sf165-testnet.json) records actual chain `4c78adac`, the
dedicated agent account, protocol 139, checksum-pinned 1.82.1 transaction CLI and
pinned 1.81.1 release compiler. The run held/asserted/renewed
`network:suifleet-testnet`, coordinated in SF-010/#11 and released the lock after
mutations. It reused the **same** non-replenishing 150,000,000 MIST allowance across
harness repairs; no faucet, new wallet or package publication was used.

The reproducible runner is `scripts/live_release_storage.py WALLET EVIDENCE_JSON
BUILD_SUI NETWORK_SUI`, with `SUIFLEET_TESTNET_LOCK_TOKEN` supplied privately. It
uses the dedicated `client-host.yaml` path alias pointing at the same agent
keystore as the authorized `client.yaml` (which names `/w/agent` inside containers).
Never print or publish the lock token or keystore. The runner executes actual
dApp upload/catalog/extension builders, verifies bytes before CLI signing and
checks successful effects, current ownership and artifact readback. Its checkpoint
files contain no keys; unknown submissions require explicit reconciliation.

- Chosen period: 12 days / 12 Walrus epochs, ending at epoch 575.
- Version publication: `DtSL8jMPgDt1x6Jrs6o6qkmcYktmWS2D2RPFRbnudhEA`.
- Wallet extension: `8gLwNu9nLhDHzw4NCEvrcGxhSFT1HuGyNWE7Sf38L3N7`, ending at epoch 577.
- Independent effects: extension charged **375,858 FROST (0.000375858 WAL)** and
  **1,011,478 MIST** gas. Entire run used **12,513,030 MIST (0.01251303 SUI)**,
  including the 1,000,000 MIST swap input. The swap received 1,000,000 FROST WAL.
- Successful artifact/recipe storage was sponsored by the public publisher:
  5,262,012 FROST reported storage cost, **not a charge to the wallet**.
- Four submitted successful transactions, plus one verifier refusal before
  signing (no gas). The receipt preserves the failed harness attempts, including
  one earlier sponsored artifact left with finite retention; they are not hidden
  by a reset allowance. A resumed run reused the known uploads/catalog/version.

Only a new disposable catalog/version and new non-deletable test blobs were
created. Immutable versions and finite-lived blobs remain as acceptance evidence;
original application objects were preserved. No firmware/OS action was performed.

## Handoff and limits

Hosted ordinary/security CI and automatic Testnet checks are pending on the pushed
head; they were not watched or polled. Pending checks are not approval. The PR
coordinator must obtain exact-head/current-base checks and independent review.
All build and network resource leases were released before Review.

Expiry dates are projections from the current Walrus epoch schedule. Artifact-byte
prices exclude explicitly labeled recipe/encryption overhead and transaction gas;
public sponsorship is not a promise of paid production service. Confidential
artifact references require explicit account-policy-authorized inspection. Old
records without an authenticated recipe digest and foreign-owned storage remain
unknown/unrenewable in the current account. Extending retention never establishes
deployment, installation, boot health or production durability.

Manual real-wallet and production-provider acceptance, physical target testing and
actual Mainnet acceptance remain unrun in [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159), under the
user-authorized implementation split. This work provisions no production
credentials and authorizes no physical programming or Mainnet writes.

## PR #356 feedback repair (2026-10-27)

The independent review found that direct `extend_blob` could move a retained
blob's end past the start of its prepaid renewal resource. The repair preserves
that boundary: Library/Deployments flag attached renewal policies, hide direct
extension and direct the operator to the Network budget renewal controls. The
fresh pre-signing read rejects those policies too. Every fleet release extension
PTB now reads `renewal` and consumes it with `option::destroy_none` before the
extension, so a concurrently attached policy aborts atomically before payment.
The Renewal introduction origin is resolved from the current package; failure
stops signing. No contract source, ABI or layout changed.

Retained publish/import/Deploy uploads whose chosen period exceeds the pool end
are refused before registration, with guidance to refill the pool or choose
wallet-paid storage. The pool read can become stale; the atomic renewal guard
still prevents stranding capacity if an unexpected post-upload extension is
needed. Such a failed upload may leave a certified finite-lived blob, but does
not register the version or change its prepaid renewal boundary.

Feedback verification:

- `node scripts/test_release_storage.cjs`: PASS, including guarded transaction
  ordering, introduction origin, missing-origin refusal, cross-page renewal
  discovery, retained direct-extension refusal and upload-period preflight.
- Pinned container `sui move test --path contracts/fleet_storage`: **76/76 PASS**,
  including the same guard aborting for an actual prepaid renewal and allowing
  an unretained blob extension to epoch 6. Host Move cache mounted into the
  disposable container; networking disabled.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node scripts/test_components_browser.cjs`:
  PASS; real browser, fixture chain/wallet/storage.

The snapshot base af805cc1736dd12849fa396d047a3ad29f99c5e0 was integrated
before the repair. No new public-network transaction was submitted for feedback;
the original publish/extension receipts above remain historical evidence for
this ticket. Current-head automatic testnet/ordinary/security CI and a fresh
independent review remain coordinator gates. Deferred manual wallet/provider,
physical and mainnet checks remain unrun in #32 and #159.

- Pinned `make dapp-typecheck`: PASS (full offline frontend prerequisites).
- Pinned `make dapp-build`: PASS, all static pages exported; existing Tailwind
  module-type warning only.
- The trailing Move command in that combined container invocation failed fetching
  dependencies with networking disabled because its Move cache was not mounted.
  The separate cache-mounted Move run above completed 76/76 tests successfully.
- `git diff --check`: PASS.

## Current-base feedback handoff (2026-10-27)

The next review reported no new actionable source defect and confirmed the
prepaid-renewal fix. Its remaining blockers were hosted CI and automatic Testnet.
The supplied snapshot additionally showed ordinary CI `check` cancelled and
base movement. Merged snapshot/current main
`85331590985644a113eb6750782121169ef03a4b` into this branch as `d70ea42c`.
The Greengrass importer merge was clean; its storage-period controls remain.
No additional functional change was necessary.

Verification after integration:

- Pinned, network-disabled `make dapp-typecheck && make dapp-build` using the
  container/mount command above: PASS; all 16 static pages exported. Existing
  Tailwind module-type warning remains.
- `node scripts/test_release_storage.cjs`: PASS.
- `node scripts/test_greengrass_compat.cjs`: PASS (90 recipe fixtures and 47
  managed-dependency fixtures).
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_greengrass_compat.py`: 40 tests PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_live_release_storage.py`: 3 tests PASS.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node
  scripts/test_components_browser.cjs`: PASS (fixture chain/wallet/storage).
- `git diff --check`: PASS.

No Move sources/tests changed in this handoff; the earlier 76/76 Move results
remain historical evidence, not a new run. No new public-network mutation was
performed. Hosted CI and automatic Testnet must run for the pushed head/current
base; they were not polled and this handoff is not approval. Fresh independent
review remains required. Deferred external acceptance remains unrun in #32/#159.

## CI process-scan repair (2026-10-27)

Failed run [40569575446](https://github.com/tyler-suifleet/suifleet/actions/runs/40569575446/job/122419887297)
reached the actual tunnel/systemd crash-cleanup check, then raised
`FileNotFoundError` reading `/proc/1031/comm`: the process exited between
`exists()` and `read_text()`. The scan now catches only missing/exited-process
errors around the read. Live matching processes remain visible and unexpected
errors still fail. No runtime daemon code or cleanup assertion changed.

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_tunneling_process_scan.py`: 2 tests PASS. The deterministic exit-during-read
  fixture reproduces the original scanner's `FileNotFoundError`; it checks both
  ENOENT/ESRCH, a surviving proxy, and permission-error propagation. Wired into
  the existing CI tunnel step and `make test-tunneling`.
- `python3 scripts/test_tunneling.py --binary
  /home/tyler/dev/codex/src/suifleet-worktrees/work-suifleet-sf-063-c57b23ee/suifleet-edge/target/debug/suifleet
  --package /tmp/pr193-tunnel.tar.gz --evidence /tmp/sf165-tunnel-repair.json`:
  all 9 actual proxy/systemd checks PASS in disposable network-none container,
  including SIGKILL descendant cleanup, TLS negatives and credential revocation.
  Uses the existing SF-063 binary, SHA-256
  `67d19a95474daf542ec4f652ae7664ffccab5d9880f82a3e379d60d7a84ca2cc`,
  and pinned proxy source `8a71618acf536923e882387e8d9eca9dbc7fdbe2`;
  this is harness verification, not a rebuild of the current daemon.
  Two earlier attempts with the old canonical-checkout cached binary failed
  before MQTT subscription because that binary has no tunnel-mode CLI support.
- Pinned network-none container command documented above, running
  `make dapp-typecheck && make dapp-build`: PASS (16 static pages).
- `node scripts/test_release_storage.cjs`: PASS.
- `git diff --check`: PASS. `git merge origin/main --no-edit`: already up to
  date; base `85331590985644a113eb6750782121169ef03a4b` is included.

No new public-network transactions or Move changes. Hosted exact-head/current-base
CI, automatic testnet and fresh independent review remain mandatory and pending;
this repair is not approval. Deferred external checks remain unrun in #32/#159.

## Current-base feedback integration (2026-10-27)

The supplied feedback has no open source findings or review threads. Its remaining
CI/testnet gates are preserved. Merged snapshot/current main
`36a85bebecee7dfcc182056cb989801a7f7728a2` as `274621ed`, bringing in the
reviewed enrollment reconciliation and bounded CI funding evidence without
conflicts or additional functional edits.

Verification after this merge:

- The pinned network-none container/mount command above, running
  `make dapp-typecheck && make dapp-build`: PASS; full offline prerequisites,
  TypeScript and all 16 static pages. Existing Tailwind module-type warning only.
- `node scripts/test_release_storage.cjs`: PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_tunneling_process_scan.py`: 2 PASS.
- Same command with `-p test_live_release_storage.py`: 3 PASS.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node
  scripts/test_components_browser.cjs`: PASS (fixture chain/wallet/storage).
- `git diff --check` and snapshot-base ancestry: PASS.

No new Move, daemon/systemd or public-network run; prior receipts remain historical.
No migration. Hosted exact-head/current-base CI and automatic Testnet are pending,
not watched or polled; fresh independent review remains required. This is a base
integration handoff, not approval. Physical/mainnet/manual-wallet/production-provider
acceptance remains explicitly unrun in #32/#159.

## Tracking-base feedback integration (2026-10-27)

Merged snapshot/current main `4d3adbee0fd77072827aeed358794990e31d370a`
without conflicts. The sole upstream change is the SF-176 hosting specification
in the seed backlog; no functional SF-165 patch or new source finding is involved.
The supplied review has no unresolved threads or actionable source findings.

Checks on the integrated branch:

- The pinned network-none container command above running
  `make dapp-typecheck && make dapp-build`: PASS, including offline prerequisites,
  TypeScript and all 16 static pages. Existing Tailwind module-type warning only.
- `node scripts/test_release_storage.cjs`: PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_tunneling_process_scan.py`: 2 PASS; same with
  `test_live_release_storage.py`: 3 PASS.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node
  scripts/test_components_browser.cjs`: PASS with chain/wallet/storage fixtures.
- `git diff --check` and snapshot/current-base ancestry: PASS.

No new Move, daemon/systemd or public-network run; prior receipts remain historical.
No migration. Hosted CI and automatic Testnet are pending on the pushed head and
were not watched or polled. Fresh independent review and exact-head/current-base
checks remain required before coordinator integration. Deferred physical/mainnet,
manual real-wallet and production-provider acceptance remains unrun in #32/#159.

## Snapshot-provider base integration (2026-10-27)

The supplied independent review reports no unresolved source/security defects or
threads; its outstanding hosted CI/Testnet gates remain required. Integrated
snapshot base `3602025ebee1954ba1042aaddb4fc299ec99e1e1` and current main
`636d9e82` without conflicts. Upstream adds the shared tracker snapshot provider
and isolates coordinator backoff fixtures. No additional SF-165 functional patch
or migration was necessary.

Verification after integration:

- Pinned network-none container command above, running
  `make dapp-typecheck && make dapp-build`: PASS, including full offline
  prerequisites, TypeScript and 16 static pages; existing Tailwind module warning.
- `node scripts/test_release_storage.cjs`: PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_tunneling_process_scan.py`: 2 PASS; same with
  `test_live_release_storage.py`: 3 PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s
  tools/pr-coordinator/tests -p test_transport.py`: 13 PASS; same with
  `test_review_throughput.py`: 9 PASS.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node
  scripts/test_components_browser.cjs`: PASS with chain/wallet/storage fixtures.

No new Move, daemon/systemd or public-network run. Previous receipts remain
historical; no new exact-head Testnet evidence is claimed. Hosted CI and automatic
Testnet are pending on the pushed head, not watched or polled. Fresh independent
review and coordinator gates remain necessary. Physical, actual-mainnet, manual
real-wallet and production-provider checks remain unrun in #32/#159.


## Subscription-tracking base integration (2026-10-27)

Integrated snapshot/current main `73d30d8620163fa47b14578b7c05b10f712cf4bc`
without conflicts. Its changes are confined to the backlog/publication records for
subscription tickets. Supplied independent feedback has no unresolved source or
security findings or threads; no additional SF-165 functional repair was needed.

Checks after integration:

- Pinned network-none container command above, `make dapp-typecheck && make
  dapp-build`: PASS, including offline prerequisites, TypeScript and static build.
- `node scripts/test_release_storage.cjs`: PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_live_release_storage.py`: 3 PASS; same command with
  `test_tunneling_process_scan.py`: 2 PASS.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node
  scripts/test_components_browser.cjs`: PASS with chain/wallet/storage fixtures.
- `make test-tracking`: 10 PASS.

No new Move, daemon/systemd or public-network run; previous Testnet receipts remain
historical, not exact-head evidence. No migration. Hosted CI and automatic Testnet
are pending on the updated head and were not watched or polled. Coordinator gates
and fresh independent review remain required. Physical, actual-mainnet, manual
real-wallet and production-provider acceptance remains unrun in #32/#159.

## Page-test tracking base integration (2026-10-27)

Integrated snapshot/current main `c4c0805c7abd8cfbfbf0d5e0df4a6f285b8da259`
without conflicts. Upstream changes only backlog/publication records. Supplied
independent feedback reports no unresolved source/security findings or threads;
no additional functional repair or migration was necessary.

Checks after integration:

- Pinned network-none container command above, `make dapp-typecheck && make
  dapp-build`: PASS, including offline prerequisites, TypeScript and static export.
- `node scripts/test_release_storage.cjs`: PASS.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p
  test_live_release_storage.py`: 3 PASS; same with
  `test_tunneling_process_scan.py`: 2 PASS.
- `make test-tracking`: 10 PASS.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node
  scripts/test_components_browser.cjs`: PASS with chain/wallet/storage fixtures.
- `git diff --check` and snapshot-base ancestry: PASS.

No new Move, daemon/systemd or public-network run. Prior Testnet receipts remain
historical, not new exact-head evidence. Hosted CI and automatic Testnet remain
pending on the pushed head and were not watched or polled. Coordinator gates and
fresh independent review remain required. Physical, actual-mainnet, manual
real-wallet and production-provider acceptance remains unrun in #32/#159.


## Shared CI harness repair (2026-10-28)

Run `40727057741` failed `check` because the dev image lacked PyYAML for
`test_testnet_prepared.py`. Its Actions CodeQL gate reported five blocking
checkout/TOCTOU findings in the merged SF-202 workflow. Reused the contained
shared repairs from PR #348 (original commits `f2ea2759` and `940cae0b`), rather
than maintaining a competing harness implementation. No scanner suppression,
wallet permission, retry or spending-policy change is included.

The dev image now installs distribution `python3-yaml`. The trusted controller
remains at its workflow SHA; a separate clone holds authenticated immutable
head/base inputs and is mounted alone into the test containers. Tests exercise
real Git history, controller preservation, rejected refs/destinations, workflow
path binding and final trusted status publication after a failed clone.

Local verification on this repair:

- `docker build -t suifleet-sf165:ci-repair --build-arg USER_UID="$(id -u)"
  --build-arg USER_GID="$(id -g)" .`: PASS.
- In that rebuilt image with `--network none`, `make test-testnet`: PASS,
  158 testnet tests, 60 operator tests and authenticated Node cache checks.
- Same image, `python3 -m unittest discover -s scripts/tests -p
  test_security_audit.py`: 23 PASS; `node scripts/test_release_storage.cjs`:
  PASS; `python3 -m unittest discover -s scripts/tests -p
  test_live_release_storage.py`: 3 PASS.
- Host offline `test_testnet*.py`: 158 tests, OK (one platform-dependent skip).
- The first frontend command completed its offline prerequisites, then pnpm
  refused the mismatched container checkout/store paths. The rerun mounts both
  at their original host paths; no source or dependency change was needed.
- Rebuilt image, network disabled, matching host checkout/store mounts:
  `make dapp-typecheck && make dapp-build`: PASS, including all offline
  prerequisites, TypeScript and 17-page production static export.

Snapshot/current base `a8516400872d70defeb7c3059798b53b4f10f84f` was already
included; `git merge origin/main` reported already up to date. No new Move,
daemon/OTA, public-testnet mutation or hardware test was run. Hosted CodeQL,
ordinary CI and automatic Testnet must validate the pushed head, followed by
fresh independent review. No hosted CI watching or polling was performed.
Manual real-wallet/production-provider, physical-device and mainnet acceptance
remains deferred and unrun in #32/#159; this repair is not approval.

## Current-base feedback integration (2026-10-28)

The independent review of `ea36e1a1` reported no actionable source/security
findings; its remaining blockers were hosted CI and automatic Testnet. The next
snapshot includes successful ordinary/security jobs for that old head, but its
Testnet status remains pending against the previous base. Those results do not
approve this integration.

Merged snapshot/current main `24b7752b95901e78b05f4afe4eac1759579b7ece`,
including the ordinary CI runtime budget and authenticated coordinator timeout
recovery. The shared YAML dependency and candidate-isolation fixes already match
main. Resolved two equivalent tunnel-probe conflicts by retaining main's function
and both existing regression invocations. No dapp, contract, edge, interface or
dependency changes were needed; no migration is required.

Verification after integration:

- Pinned `suifleet-sf165:ci-repair`, network disabled, checkout and pnpm store
  mounted at their original host paths: `make dapp-typecheck && make dapp-build`
  PASS, including 158 Testnet fixture tests, 60 operator tests, frontend offline
  prerequisites, TypeScript and all 17 static pages. An existing optional
  prerequisite test was skipped; no live transaction was submitted.
- Same container: `node scripts/test_release_storage.cjs` PASS;
  `python3 -m unittest discover -s scripts/tests -p test_live_release_storage.py`
  3 PASS; `-p 'test_tunneling_process*.py'` 4 PASS;
  `python3 -m unittest discover -s tools/pr-coordinator/tests -p test_job_timeouts.py`
  9 PASS (offline authenticated timeout fixtures).
- Host `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_security_audit.py`: 23 PASS. CI YAML parses, retains the 120-minute
  check budget and invokes both tunnel regression suites. `git diff --check`
  and snapshot-base ancestry PASS.
- Host `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s
  tools/pr-coordinator/tests`: all 422 tests PASS (offline fixtures).

Hosted CI/automatic Testnet for the new pushed head remains pending and was not
watched or polled. Fresh independent review and successful exact-head/current-base
checks remain required. No new Move/OTA, browser, public-network or physical test
was run; historical Testnet publication/extension receipts are unchanged. Manual
real-wallet/production-provider, physical and actual-mainnet acceptance remains
unrun in #32/#159. No deployment, hardware programming or production credentials.

## Wallet hydration base integration (2026-10-28)

The review of `8bb6da80` found no unresolved source/security defect; CI and
current-base automatic Testnet remained approval gates. The feedback snapshot's
ordinary/security jobs succeeded for that older head, while its Testnet status
was still pending against the previous base. Those results are not approval of
the new integration.

Merged snapshot/current main `935e96f84c2f74c7b67b98a3181a7520c98b4914`
in `6834a574`. Resolved the Deployments import conflict by preserving release
storage controls and using main's hydration-safe account hook. Applied that same
hook to the three branch-owned account readers (`useReleasePeriod`,
`ReleaseStorageStatus` and `ConfidentialRecipeStorage`) so saved-wallet restoration
uses the shared rendering boundary. Transaction-time authority/network/effects
checks remain in place. No contract, dependency or migration changes.

Verification after integration:

- Pinned `suifleet-sf165:ci-repair`, network disabled, checkout/store mounted at
  original host paths: `make dapp-typecheck && make dapp-build` PASS, including
  158 Testnet fixture tests, 60 operator tests, frontend prerequisites, TypeScript
  and the 17-page production static export. One existing optional prerequisite
  test skipped. Existing Tailwind module-type warning remains nonfatal.
- Same container: `node scripts/test_release_storage.cjs` PASS;
  `python3 -m unittest discover -s scripts/tests -p test_live_release_storage.py`
  3 PASS.
- Host Node 24.21.0, Chrome `/usr/bin/google-chrome`:
  `scripts/test_components_browser.cjs`, `scripts/test_upload_browser.cjs` and
  `scripts/test_wallet_browser.cjs` all PASS with chain/wallet/storage fixtures.
- Same network-disabled pinned container: `make test-hydration` PASS for default
  and Seal-Testnet exports. Every console route passed logged-out and saved-wallet
  reconnection cases; the deliberate hydration-mismatch negative control was
  detected in each profile. Seal-Testnet production build also PASS.
- `git diff --check`, Tyler author/committer identity and snapshot/current-base
  ancestry PASS.

No new Move/OTA or public-network transaction was run. Historical chosen-period
publish/extension receipts remain unchanged. Hosted CI and automatic Testnet must
run for the pushed head; they are not watched or polled by this worker. Fresh
independent review remains required. Manual real-wallet/production-provider,
physical and actual-mainnet acceptance remains unrun in #32/#159. No merge,
deployment, hardware programming or production credentials.


## Current CI/site base integration (2026-10-28)

The feedback snapshot for `2dccb5c7` contains no unresolved source/security
findings and no open threads. Its ordinary/security checks succeeded, but its
pending automatic Testnet status names the older base `935e96f8`; it is not
current-base acceptance. Merged snapshot/current main
`95ac2b202db17014d691eb1646d955829aa2200a` without conflicts. Upstream changes
are CI/Testnet preparation, proof checks, site and documentation; no upstream
application, contract, daemon or interface delta. Release storage behavior and
its existing migration boundary are preserved.

Verification after integration:

- `suifleet-sf165:ci-repair`, network disabled, identical host checkout/store
  mounts: `make dapp-typecheck && make dapp-build` PASS. Includes 161 offline
  Testnet harness tests, 60 operator tests, manifest/frontend prerequisites,
  TypeScript and the production static export. Existing optional prerequisite
  skip and nonfatal Tailwind module-type warning remain.
- Same container: `node scripts/test_release_storage.cjs` PASS and
  `python3 -m unittest discover -s scripts/tests -p test_live_release_storage.py`
  3 PASS.
- Host Chrome `/usr/bin/google-chrome`, `CHROME_EXECUTABLE` set:
  `node scripts/test_components_browser.cjs`, `node scripts/test_upload_browser.cjs`
  and `node scripts/test_wallet_browser.cjs` all PASS (fixture chain/wallet/storage).
- Host `python3 -m unittest discover -s site/tests -p test_site.py`: 18 PASS.
- Host `python3 -m unittest discover -s scripts/tests -p 'test_tunneling_process*.py'`:
  4 PASS.

No new Move, OTA, hydration-profile or public-network test was run in this repair.
Historical chosen-period publish/extension evidence remains unchanged; no new
exact-head automatic Testnet success is asserted. Hosted CI/Testnet are pending
verification after push and were not watched or polled. The coordinator must
require their actual exact-head/current-base success and fresh independent review.
Manual real-wallet/production-provider, physical and actual-mainnet acceptance
remains unrun in #32/#159. No deployment, hardware or production credentials.


## Current-base checkout repair integration (2026-10-29)

The feedback snapshot has no unresolved source/security findings or open threads.
Its ordinary/security checks succeeded for `512ca1b2`, but automatic Testnet
reported an error against old base `95ac2b20`. That is not current-base acceptance.
Merged snapshot/current main `b3994cf9522502365df1e4fa7021bb030639f0ea`
without conflicts. The upstream delta changes only `scripts/testnet_ci.py` and
`scripts/tests/test_testnet_prepared.py`: shallow controllers now unshallow before
copying authenticated candidate history. SF-165 application and contract code is
unchanged; no migration is required.

Post-integration checks:

- `suifleet-sf165:ci-repair`, network disabled, identical host checkout/store mounts:
  `make dapp-typecheck && make dapp-build && node scripts/test_release_storage.cjs
  && python3 -m unittest discover -s scripts/tests -p test_live_release_storage.py`:
  all PASS, including 162 offline Testnet tests, 60 operator tests, TypeScript,
  production static export and 3 live-release harness tests. Existing optional
  prerequisite skip and nonfatal Tailwind module-type warning remain.
- Host `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_testnet_prepared.py`: 9 PASS, including real shallow clone, missing
  candidate, independently advanced base and hook/filter nonexecution cases.
- Host `CHROME_EXECUTABLE=/usr/bin/google-chrome node` with
  `scripts/test_components_browser.cjs`, `scripts/test_upload_browser.cjs` and
  `scripts/test_wallet_browser.cjs`: all PASS, fixture chain/wallet/storage only.

Hosted exact-head/current-base CI and automatic Testnet remain pending on the
new pushed revision; they are not watched or polled by this worker. The coordinator
must enforce actual success and obtain fresh independent review. Historical chosen
period publication/extension receipts remain historical evidence, not a new run.
No new Move, OTA, public-network or profile hydration run was performed.
Manual real-wallet/provider, physical and actual-mainnet acceptance remains
explicitly unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159).

## OTA runner base integration (2026-10-29)

The supplied review of `1461e587` reports no unresolved code/security findings
and no open review threads. The feedback snapshot records ordinary/security CI
success for that head, but automatic Testnet is pending against older base
`b3994cf9`; neither establishes acceptance against the newly advanced base.
Merged snapshot/current main `9d1b68cd2df85b3d7dbf2a56897d1b07a2b2e106`
without conflicts in `5d04afea`. Upstream changes are the bounded OTA suite runner,
its Makefile wiring, operational documentation and tracking records. The application,
contracts and interfaces are unchanged from the independently reviewed head.
No migration is required.

Post-integration checks:

- `suifleet-sf165:ci-repair`, network disabled, checkout and pnpm store mounted at
  identical host paths: `make dapp-typecheck && make dapp-build && node
  scripts/test_release_storage.cjs && python3 -m unittest discover -s scripts/tests
  -p test_live_release_storage.py`: PASS, including offline prerequisites,
  TypeScript, production static export, release storage regressions and 3 Python
  harness tests. Full command output retained locally at
  `/tmp/sf165-current-base-checks.log`. Optional prerequisite skips remain explicit.
- Host `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_ota_suites.py`: 6 PASS, including bounded overlap, failure propagation,
  timeout, process-group interruption and complete suite inventory. The printed
  failure/timeout diagnostics are expected assertions of the negative cases.
- Host `CHROME_EXECUTABLE=/usr/bin/google-chrome node` with
  `scripts/test_components_browser.cjs`, `scripts/test_upload_browser.cjs` and
  `scripts/test_wallet_browser.cjs`: all PASS; chain/wallet/storage are fixtures.
- `git diff --check`: PASS. Merge author and committer are Tyler Bush.

Hosted exact-head/current-base CI and automatic Testnet remain pending for the new
push. They were not watched or polled; the coordinator must enforce actual success
and obtain fresh independent review. No new Move, full OTA, profile hydration or
public-network transaction was run. Historical publication/extension receipts are
unchanged, not new-head automatic Testnet evidence. No network mutation lock was
needed. Manual real-wallet/provider, physical and actual-mainnet acceptance remains
explicitly unrun in [#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[#159](https://github.com/tyler-suifleet/suifleet/issues/159).


## Current-base integration after independent approval (2026-10-29)

The supplied independent review approved f5fba837 against 9d1b68cd and reported
no actionable source/security findings or open threads. The snapshot base advanced
to 0189768b8216a966545f193919956a7495e25d27. Merge 0a65f122 integrates
that base cleanly; its actual delta contains only the public site, brand assets
and tracking records. SF-165 dapp, scripts, contracts and interfaces are unchanged.
No functional feedback patch or migration is required.

Post-integration checks:

- Network-none `suifleet-sf165:ci-repair`, user UID:GID, checkout/store mounted
  at their identical host paths, `INSIDE_DEV_CONTAINER=1`:
  `make dapp-typecheck && make dapp-build && node scripts/test_release_storage.cjs
  && node scripts/test_seal_frontend.cjs && node scripts/test_walrus_publishers.cjs
  && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_live_release_storage.py`: PASS, including offline prerequisites,
  TypeScript, static production export, release/Seal/publisher regressions and
  three Python tests. Input-based prerequisite skips remain explicit.
  Local log: `/tmp/sf165-base0189768-checks.log`.
- `make site-check test-site`: PASS, built site checker plus 19 offline tests.
  Local log: `/tmp/sf165-site-checks.log`.
- `git diff --check`: PASS.

Hosted CI and automatic Testnet for the new pushed head remain pending coordinator
verification; no hosted CI was watched or polled. The supplied prior-head Testnet
success refers to base 9d1b68cd and is not evidence for this new head/base. No new
Move/full OTA run or public-network mutation was performed. Existing chosen-period
publication and extension receipts remain historical evidence. Manual real-wallet,
production-provider, physical and actual-mainnet acceptance stays explicitly unrun
in #32 and #159. Next: coordinator exact-head/current-base ordinary/security CI,
automatic Testnet and fresh independent review. No merge or deployment performed.


## Preparation-routing base integration (2026-10-29)

The supplied independent review of `840dd8ee` reported no actionable source or
security defects and no open threads. The feedback snapshot shows its ordinary
CI and automatic Testnet succeeded, but Testnet binds base `0189768b`, while
the snapshot/current base is `1a821c116bb210440a1f005a7662a01bc3b2d57a`.
Merge `3f69670a` integrates that base cleanly. Upstream changes authenticate
preparation error receipts and retain bounded coordinator repair ownership.
SF-165 application, contracts and interfaces are unchanged; no migration needed.

Post-integration verification:

- Network-none `suifleet-sf165:ci-repair`, user UID:GID, checkout and pnpm store
  at identical host paths, `INSIDE_DEV_CONTAINER=1`: `make dapp-typecheck &&
  make dapp-build && node scripts/test_release_storage.cjs && node
  scripts/test_seal_frontend.cjs && node scripts/test_walrus_publishers.cjs &&
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_live_release_storage.py`: PASS, including offline prerequisites,
  TypeScript, static export and three Python tests. Input-based prerequisite
  skips remain explicit. Log: `/tmp/sf165-base1a821-checks.log`.
- Host `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_testnet.py`: 45 tests PASS. Log: `/tmp/sf165-testnet-unit.log`.
- Host `CHROME_EXECUTABLE=/usr/bin/google-chrome node` with
  `scripts/test_components_browser.cjs`, `scripts/test_upload_browser.cjs` and
  `scripts/test_wallet_browser.cjs`: all PASS; chain/wallet/storage are fixtures.

New-head hosted ordinary/security CI and automatic Testnet remain pending
coordinator verification, followed by fresh independent review. No hosted checks
were watched or polled. No new Move/full OTA or public-network mutation ran; no
mutation lock was needed. Historical chosen-period publish/extension receipts
remain unchanged and are not new-head automatic Testnet evidence. Manual
real-wallet/production-provider, physical and actual-mainnet acceptance remains
explicitly unrun in #32 and #159. No deployment or hardware programming performed.

- Host `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s
  tools/pr-coordinator/tests`: 439 tests PASS in 203.457s; expected negative-case
  diagnostics occur in the log `/tmp/sf165-coordinator.log`.
- `git diff --check`: PASS; snapshot base ancestry verified.

## Guide-recording base integration (2026-10-29)

The independent review of `44882be8` reports no actionable source/security findings
or open threads. Its remaining gates are exact-head/current-base hosted CI,
automatic Testnet and fresh evidence review. The supplied feedback snapshot shows
ordinary CI successful and Testnet pending for the prior base `1a821c11`.
Merge `fbc5e6fc` cleanly integrates snapshot/current main
`9e22b599916bd838921eeb9cb98d643f133bca53`, including reviewed guide recordings.
SF-165 application, contracts and interfaces are unchanged; no migration needed.

Post-integration checks:

- Network-none `suifleet-sf165:ci-repair`, UID:GID, checkout and pnpm store mounted
  at identical host paths, `INSIDE_DEV_CONTAINER=1`: `make dapp-typecheck &&
  make dapp-build && node scripts/test_release_storage.cjs && node
  scripts/test_seal_frontend.cjs && node scripts/test_walrus_publishers.cjs &&
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_live_release_storage.py`: PASS, including offline prerequisites,
  TypeScript, production static export and three Python tests. Input-based
  prerequisite skips remain explicit. Log: `/tmp/sf165-base9e22-checks.log`.
- Host `make site-check test-site`: PASS, site integrity and 25 tests.
  Log: `/tmp/sf165-base9e22-site.log`.
- Host `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
  -p test_testnet.py`: 45 tests PASS. Log: `/tmp/sf165-base9e22-testnet.log`.
- Host `CHROME_EXECUTABLE=/usr/bin/google-chrome node` with
  `site/tests/recording_browser.cjs`, `scripts/test_components_browser.cjs`,
  `scripts/test_upload_browser.cjs` and `scripts/test_wallet_browser.cjs`: all
  PASS; chain/wallet/storage fixtures only.
- `git diff --check`: PASS; snapshot base ancestry verified.

Hosted ordinary/security CI and automatic Testnet remain pending on the new head;
not watched or polled. No new Move/full OTA run or public-network mutation ran.
Historical chosen-period publication/extension receipts are not new-head CI
results. Manual real-wallet/production-provider, physical and actual-mainnet
acceptance remains explicitly unrun in #32 and #159. Next: coordinator enforces
actual exact-head/current-base CI/Testnet and fresh independent review.

## Rollout-builder base integration (2026-10-29)

The independent review of `64713bd1` reports no actionable source/security
findings or open threads. The supplied snapshot records ordinary CI success,
but automatic Testnet is pending against the previous base. Merge `d245bba3`
cleanly integrates snapshot/current main
`6e45938a8cefcc5cd95f5c96b0dffff7f2b4c649`, including the rollout builder and
testnet harness changes. SF-165 storage controls and renewal guards are unchanged.
No contract/API/dependency migration is needed.

Browser verification on the merged branch: host
`CHROME_EXECUTABLE=/usr/bin/google-chrome node` with
`scripts/test_rollout_builder_browser.cjs`, `scripts/test_components_browser.cjs`,
`scripts/test_upload_browser.cjs` and `scripts/test_wallet_browser.cjs`: all PASS.
Chain/wallet/storage/signing use fixtures.

An initial network-none container run passed the offline prerequisites, then
pnpm refused the relocated `/workspace` dependency mount before typechecking
(`ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`). The retry mounts the checkout
and pnpm store at their identical host paths. This is a validation environment
correction, with no source/dependency change.

Post-integration retry: network-none `suifleet-sf165:ci-repair`, UID:GID,
identical host checkout/pnpm-store mounts and `INSIDE_DEV_CONTAINER=1`:
`make dapp-typecheck && make dapp-build && node scripts/test_release_storage.cjs
&& node scripts/test_seal_frontend.cjs && node scripts/test_walrus_publishers.cjs
&& node scripts/test_rollout_form.cjs && PYTHONDONTWRITEBYTECODE=1 python3 -m
unittest discover -s scripts/tests -p test_live_release_storage.py &&
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
-p test_testnet.py`: PASS, including offline prerequisites, TypeScript,
production static export, three release-storage and 45 testnet-harness tests.
Input-based prerequisite skips remain explicit. Log:
`/tmp/sf165-base6e459-checks-retry.log`. `git diff --check`: PASS.

Hosted ordinary/security CI and automatic Testnet remain pending on the new head;
not watched or polled. No new Move/full OTA or public-network transaction ran.
Historical chosen-period publication/extension receipts remain historical, not
new-head CI evidence. The retained stale-pool path remains fail-closed: a
finite-lived uploaded blob may remain, but no version registers or prepaid
boundary moves. Manual real-wallet/production-provider, physical and actual
mainnet acceptance remains explicitly unrun in #32 and #159. Next: coordinator
enforces actual exact-head/current-base CI/Testnet and fresh independent review.

## Enrollment-budget base integration (2026-10-29)

The supplied independent review approves the previous head with no actionable
source/security findings or open threads. Its successful hosted checks and
Testnet status apply to base `6e45938a`, rather than the new snapshot base.
Merge `e27a6a8f` integrates current main
`1bebb3451d92e6b56b2fb305eb4680ae4d11e09c` cleanly, including the enrollment
budget checks. Release storage behavior is unchanged; no migration is needed.

Network-none `suifleet-sf165:ci-repair`, UID:GID, identical host checkout and
pnpm-store mounts, `HOME=/tmp`, `INSIDE_DEV_CONTAINER=1`:
`make dapp-typecheck && make dapp-build && node scripts/test_release_storage.cjs
&& node scripts/test_seal_frontend.cjs && node scripts/test_walrus_publishers.cjs
&& node scripts/test_fleet_budget.cjs && node scripts/test_rollout_form.cjs &&
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests
-p test_live_release_storage.py`: PASS, including offline prerequisites,
TypeScript, production static export and three release-storage signer tests.
Log: `/tmp/sf165-base1bebb-checks.log`. Existing Tailwind module-type warning
remains non-fatal. Host `CHROME_EXECUTABLE=/usr/bin/google-chrome node` with
`test_onboarding_browser.cjs`, `test_components_browser.cjs`,
`test_upload_browser.cjs` and `test_wallet_browser.cjs` under `scripts/`: all
PASS, using chain/wallet/storage fixtures. `git diff --check`: PASS.

Hosted CI/automatic Testnet remain pending for the new pushed head and are not
watched or polled. No new Move/full OTA/public-network transaction ran; original
publication/extension effects and costs remain historical receipts. No mutation
lock was needed. Manual real-wallet/provider, physical and actual-mainnet
acceptance remains unrun in #32/#159. Next: coordinator exact-head/current-base
CI/Testnet gates and fresh independent review before serial integration.
