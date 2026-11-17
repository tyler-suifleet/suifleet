# SF-274 guide workflow audit

## Navigation compatibility repair (PR #777 review)

The original audit below predates PR #715 and alone does not establish current
compatibility. Reverification uses the exact integration base
`9ede9cce7aac0d9903d0364153562d62ac69b6db`, now included in this source branch.
The guide is an edited demonstration, not a manual Slush test.

The current sidebar and page titles are **Software library** (`/components`)
and **Deploy software** (`/firmware`). The written instructions and future
recorder selectors now use those names. Choosing Deploy software without a
library version opens a library-first page: choose **Choose or publish a
version**, then **Deploy this version**. The direct-file form is optional under
**Advanced: deploy a file directly**; it is not the route used by the group clip.

The publish and deploy lessons are regenerated with current action cards,
female narration and optional VTT captions. Their speech explicitly identifies
**Components** and **Deploy component** as the names visible in the older
recording and maps them to today's names. This is a documented compatible
historical capture, not a relabelled fresh recording. Original `console_commit`,
transaction provenance and footage hashes remain intact in `videos.json`.

Compatibility was checked against the actual source delta from the prior source
head `3c30a7b2` to the pinned base: the library page changes its title; Publish
version, Set up publishing, ComponentPublisher and Deploy this version remain.
The deployment page changes its title and adds the library-first entry and
Advanced wrapper. A selected catalog version still opens target review; group
selection, desired-state controls and the deployment builder are unchanged by
PR #715. No direct-file Artifact/Recipe footage is being presented as the new
library-first entry flow.

New regression coverage compares every guide route/console label with NAV in
`ConsoleNavigation.tsx`. The real exported-console browser check visits all ten
guide entry routes and checks their visible navigation labels, plus the two
renamed page headings. It is signed-out and uses blocked/offline external
responses; it does not establish connected-wallet or chain behavior.

## Original ten-stage source audit

The original comparison used console source
`b9bb2ef6210624d732ab2de2075ea1826e5300c1`. Historical command results below
are retained as historical evidence, not rerun results for this repair.

| Lesson | Customer behavior and source checked | Media decision |
|---|---|---|
| Sign in | Wallet connection, first fleet transaction, selected workspace (`dapp/src/app/get-started`, fleet selector). First-device overview now lists the required route and optional branches. | Retain compatible sign-in footage/speech. |
| Capacity | Device allowance is separate from operating funds; review offer, prepay, apply due payment (`dapp/src/app/billing`, `docs/SUBSCRIPTIONS.md`). | Retain. Test tariff remains explicitly illustrative. |
| Budget | Deposit pays device activity, not capacity or proof of online status (`dapp/src/app/budget`, `docs/FLEET_BUDGET.md`). | Retain. |
| Register | Public address only; install/configure client separately and wait for a signed report (`dapp/src/app/devices`, `docs/INSTALL_DEVICE_CLIENT.md`). | Update next-step narration to make optional branches explicit. |
| Batch enrollment | Optional alternative to individual registration; credentials remain secret, single-use and device-specific (`dapp/src/app/enrollment`, `docs/ENROLLMENT.md`). | Update optional introduction and next-step card. |
| Private access | Create group and membership first; grant decryption before confidential publication (`dapp/src/app/access/page.tsx`). Policy can also grant accounts/devices; the lesson deliberately demonstrates a group. | Move before publication; update prerequisite speech and next card. Existing capture reuses its group/policy. |
| Publish | Confidential encrypts artifact and recipe; Public explicitly acknowledges unencrypted content. Policy and configured encryption service precede private upload (`ComponentPublisher.tsx`, `DeliveryFields.tsx`). Publication installs nothing. | Update policy prerequisite speech and lesson number. Public-file capture is retained with the explicit historical/current label mapping above. |
| Deploy | Library and direct-upload flows both support group desired state. Individual devices require clearing desired state (`dapp/src/app/firmware/page.tsx`, `buildCatalogDeploymentTx`). Applied is installer completion, not ongoing health. | Update individual-device/desired-state speech and lesson number. Library group capture is retained with the explicit label mapping above. |
| Staged rollout | Explicit creator-signed Check device health and continue; on-chain gates run on each check. No background signer or automatic batch advancement. Admission is not a liveness attestation (`dapp/src/app/rollouts/page.tsx`, `docs/STAGED_ROLLOUTS.md`). | Update narration and next card. Retain create/pause footage; no admission/completion claimed. |
| Fleet settings | Save cadence/estimates, with budget funding separate; devices pick up at next check (`dapp/src/app/settings`, `docs/FLEET_SETTINGS.md`). | Retain. |

## Compatible footage and production boundary

The input is the caption-free media at Git
`d21a875750fbc657b1a487e58eb221669202e5f8`. Its manifest is dated 2026-11-05
and retains the actual per-clip console revisions below. It is a compatible
multi-revision capture set, not one freshly recorded build.

| Captured lessons | Console revision |
|---|---|
| Sign in, capacity | `ad3da5a98f79ec769aad46f7fc6109fcbd6e65ea` |
| Budget, register | `3586b750c9e722cd7d8c0473c0f41102af46c455` |
| Batch enrollment | `77632f6790f184e38858cdbe67a8a1aebf1a7f2b` |
| Publish | `ac6c608b715315d6cd0366244f5641bb52a418ea` |
| Deploy, rollout, access, settings | `88d2364047a358f0a99bb1c1cb4f98744d5cf017` |

The changed lessons use those same action intervals and visible cursor, new local
Kokoro `af_heart` female speech/cards, and new optional WebVTT captions. This is
compatible historical capture, not a current-build recapture. Public example,
capacity checkout, and Applied receipt provenance remain in `videos.json`.
The access capture’s group already exists; its new introduction directs new
customers to create/add members first. It does not pretend to show that action.
The individual-device alternative and manual advancement are explanations, not
claims that this footage demonstrates those signed transactions.

Only register-device, enroll-batch, access-policy, publish-version, deploy-group
and staged-rollout are regenerated. The other four clips, captions and posters
retain their exact hashes. The manifest retains original production provenance
and records the selective refresh. No paid synthesis, testnet writes or faucet
requests are needed: chain spending for this refresh is zero. Existing 15 MB per
format/clip limits apply. No new wallet, Pi, Mainnet or provider acceptance is
claimed; final external checks remain [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).

## Validation environment and limits

Local tools: Python 3.10, Chrome 153.0.8010.36, Node 24.21.0 and pnpm 12.14.0
for the host browser/synthesis setup; locked frontend dependencies were installed
with `corepack pnpm install --frozen-lockfile`. The additional application checks
used pinned Node 22.22.2 in `suifleet-dev:latest` with network disabled, invoking
`node node_modules/typescript/bin/tsc --noEmit` then
`node node_modules/next/dist/bin/next build`: both passed. No dependency versions
or lockfiles were changed. The pnpm 11 wrapper initially refused the host pnpm 12
modules layout; direct pinned-runtime invocation avoids modifying that layout.

Host `pnpm typecheck` passed. The first host `pnpm build` generated the static
export then exited 1 at a Next.js advisory reminder; its normal retry exited 0
while preserving the warning. `pnpm audit --json` exited 1 with two moderate
baseline advisories: Next 16.4.1 (GHSA-mfr8-8qww-v76h) and transitive PostCSS
8.5.18 under `@tailwindcss/postcss` (GHSA-fxqj-rqcc-2cmp). This is **not** clean
security acceptance. No scanner suppression or unrelated dependency upgrade was
performed. The finding is recorded in [SF-274’s existing issue](https://github.com/tyler-suifleet/suifleet/issues/707#issuecomment-6928235870)
for coordinator triage under the instruction not to create additional issues;
normal security/CI gates remain enforced.

Local verification commands:

- `PYTHONPATH=/tmp/sf274-python OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 python3 site/guide/render_lessons.py --source-media /tmp/sf274-source/site/media/guide --runtime /tmp/sf274-render --output /tmp/sf274-produced-v2 --steps register-device,enroll-batch,access-policy,publish-version,deploy-group,staged-rollout` passed. Both formats of all six changed lessons fully decoded, with female audio, 1280×800 dimensions and file limits checked. The largest published clip is 3,412,794 bytes, below 15,000,000. Four retained lessons remain byte-identical. Sampled contact sheets spanning each changed lesson were visually inspected; this is not a claim of continuous real-time human viewing.
- `python3 site/build.py --out /tmp/sf274-site` and `python3 site/check.py /tmp/sf274-site --require-videos` passed (77 built files).
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s site/tests -v` passed 69 tests, including workflow order, optional paths, source/hash checks, and complete caption contents/timing.
- `node scripts/test_component_catalog.cjs`, `node scripts/test_desired_state.cjs`, `node scripts/test_rollout.cjs` and `node scripts/test_rollout_form.cjs` passed: catalog desired-state and one-shot PTBs, group-only restrictions, policy/cohort mapping, on-chain advance and failure handling. These are software regressions, not live transaction acceptance.

The initial real-browser runs exposed a comparison bug: native captions preserve
a legal line wrap inside “post-apply”; the old whitespace comparison inserted a
space in that compound. The shared test helper now reverses that presentation
wrap while still rejecting a real inserted space. It does not skip cue, wording
or timing assertions, and no media bytes were changed to satisfy that test.

Final real-player checks passed:

- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/guide_browser.cjs /tmp/sf274-site /tmp/sf274-browser-shots`: all 79 instructions at 1440×900, 1280×720, 390×844, 375×667, 320×568, 844×390 and 640×360, with no page/text overflow; keyboard navigation, history/deep links, no-JavaScript/blocked-JavaScript fallback, playback, inactive-video pause, MP4/404 fallbacks and native captions passed. Desktop and phone screenshots were visually inspected.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/guide_captions_browser.cjs /tmp/sf274-site`: all ten real players, all 74 cue texts and seeked active cue midpoints at 390px and 1440px; captions off by default/on/off, timing, encoded dimensions, versioned URLs and actual app/intro/outcome frames passed.
- `git diff --check` passed.

These checks serve the real local static site and browser media decoder. They
make no wallet or chain calls and do not establish live public-site publication,
Slush/extension-wallet acceptance, physical mobile/Safari, Pi or Mainnet results.
Hosted CI and independent review are pending after push and remain merge gates.
No contract, daemon, transaction-builder or dependency source changed, so no
additional local-chain or machine-wallet mutation was needed for this guide fix.

## PR #777 navigation compatibility repair (2026-11-17)

Reverified against exact integration base `9ede9cce7aac0d9903d0364153562d62ac69b6db`,
which includes PR #715. The source branch incorporates that exact base; it is
not a fresh capture. Publish/deploy speech and optional captions now explicitly
translate the old on-screen names to **Software library** and **Deploy software**.
Written instructions use current labels and explain the library chooser versus
**Advanced: deploy a file directly**. The two clips were regenerated with local
`af_heart` speech; their original action intervals, console revisions and signed
transaction provenance remain intact. Eight other lessons retain their exact
media hashes. `videos.json` records the checked console-source hashes and this
compatibility decision. No video frame was relabelled to imply a current capture.

Checks run for this repair (host Python 3.10.12, Node 24.21.0, Chrome 153.0.8010.36):

- `node dapp/node_modules/typescript/bin/tsc --noEmit -p dapp/tsconfig.json`: passed.
- `(cd dapp && NEXT_TELEMETRY_DISABLED=1 node node_modules/next/dist/bin/next build)`: passed, static export generated; existing module-type warning remains.
- `PYTHONPATH=/tmp/sf274-python OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 python3 site/guide/render_lessons.py --source-media /tmp/sf274-source/site/media/guide --runtime /tmp/sf274-nav-render --output /tmp/sf274-nav-produced --steps publish-version,deploy-group`: passed. Both MP4/WebM formats fully decoded; female speech, optional VTT and 1280×800 dimensions checked. Durations 55.20 and 80.04 seconds; largest file 3,655,881 bytes, below 15,000,000. Sampled actual encoded action frames were visually inspected.
- `python3 -m unittest discover -s site/tests`: 70 passed, including all ten route/label comparisons against NAV.
- `python3 site/build.py --out /tmp/sf274-nav-site` and `python3 site/check.py /tmp/sf274-nav-site --require-videos`: passed, 77 files.
- `python3 site/prepare_hosted.py console /tmp/sf274-nav-export /tmp/sf274-nav-console --revision 9ede9cce7aac0d9903d0364153562d62ac69b6db`, then `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/console_browser.cjs /tmp/sf274-nav-console`: passed. Input was this branch's real static export plus the reviewed testnet profile manifest for offline CSP generation. All ten visible entry labels and both renamed headings passed. External responses are offline fixtures, with no wallet connection or provider availability claim.
- `cargo +1.94.0 test --locked --manifest-path suifleet-edge/Cargo.toml`: 477 passed, zero failed, four ignored (93.72 seconds).
- `make RUN= test-testnet`: passed; Python groups 5/7/4/4/32/27/221/83, one existing installed-container identity skip; authenticated-cache and receipt-effects Node checks passed. Transactions printed by these tests are offline fixtures.

The recorder's read-only preflight was attempted on the host but refused before
any network/write operation because the release `sui` executable is absent from
PATH. No fixture/chain preflight success, new capture, faucet, signature or chain
spending is claimed. This compatibility repair instead verifies source behavior
and the current signed-out export, retaining historical signed-flow provenance.
Automatic exact-candidate public-testnet validation, security CI and fresh
independent review remain required. Deferred checks in #32/#159 remain unrun.

Current real-player checks also passed:

- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/guide_browser.cjs /tmp/sf274-nav-site /tmp/sf274-nav-browser-shots`: all 79 instructions at seven desktop/mobile/landscape sizes; no overflow, keyboard/history/deep links, disabled/blocked-JavaScript fallback, native captions, playback/pause and MP4/error fallback passed.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/guide_captions_browser.cjs /tmp/sf274-nav-site`: all ten players and every native caption cue/timing at 390px and 1440px, off/on/off behavior, dimensions, versioned media and intro/action/outcome frames passed.
- Host build checks: `node dapp/node_modules/typescript/bin/tsc --noEmit -p dapp/tsconfig.json` and `(cd dapp && NEXT_TELEMETRY_DISABLED=1 node node_modules/next/dist/bin/next build)` passed on Node 24.21.0. Existing module-type warning remains. No dependency or lockfile changes. Chrome 153.0.8010.36 and Python 3.10.12 were used for these checks.

Initial player invocations ran before the built-site directory existed and
failed with missing `_headers`; the completed-build reruns above passed. This
was test setup ordering, with no fallback or assertion weakened. This repair
uses compatibility reverification and explicit spoken historical/current label
mapping rather than claiming a fresh testnet capture. Independent review must
assess this documented compatibility boundary.
