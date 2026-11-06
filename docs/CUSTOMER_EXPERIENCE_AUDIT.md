# Customer experience audit — 4 November 2026

Implementation follow-through: [current remediation and deployment evidence](CUSTOMER_AUDIT_REMEDIATION.md). The conclusions below describe the original audit date.

## Verdict

The core story is understandable: install a device client, register devices, publish software, deploy it and inspect the result. The public preview is **not yet a self-service customer onboarding experience**. A prospective customer can encounter configuration and capacity barriers that look like product failures. Clearer copy helps, but does not provision those services.

This audit assumes no blockchain knowledge and little Greengrass knowledge. It evaluates discovery, prerequisites, terminology, task order, empty/error states, reporting expectations and instructional media. It does not establish production security, legal compliance, paid-provider readiness or hardware acceptance.

## Follow-through: mobile preview and onboarding (SF-246)

The local-only addresses above were unsuitable for a phone. The evaluation website, app and curated documentation now have verified public HTTPS preview URLs, and all onboarding help links use the configured documentation origin. The site builder/checker accept a constrained public-origin configuration instead of post-build URL rewriting.

The testnet evaluation core was compatibly upgraded to version 3, preserving the registry and every previously recorded type origin. Reporting/storage locators and subscription descriptors are now configured explicitly. Both immutable checkout offers passed the actual frontend readers. The checkout uses **0.01 test SUI or test USDC for a 100-device demonstration**, not commercial pricing; the UI labels the test tariff/conversion and canonical commercial pricing remains unchanged.

A funded disposable identity created an organization, executed the actual frontend SUI prepay and collection builders, and reached 100 devices of verified capacity. This exposed an important two-step explanation: prepay first, then apply the due prepaid payment to activate capacity. Billing now explains that the latter uses the prepaid balance, costs a network fee, and does not debit the wallet again. It refreshes fleet capacity after confirmed changes.

The first-use screen now checks capacity, account fee funds and device operating funds separately. Missing capacity or administrator authority blocks registration guidance before a doomed approval request; existing inventory remains registered. Unknown/failed budget reads are not presented as funded or empty successes.

A deeper remaining feature gap is tracked by SF-247 / #616: reporting/storage provisioning and locator discovery for a **new** fleet currently require operator configuration. A verified evaluation binding does not solve self-service provisioning for every new customer. SF-245 / #612 owns fresh current-UI recordings and native captions; an unmerged recording source alone is not a mandatory merge wait, and the operator explicitly permits the current PR stack.

Existing pinned device clients need the generated version-3 connection settings in a staged rollout. Their keys/journals were not changed, and this work does not claim physical Pi rollout or manual phone-wallet acceptance. Dedicated-agent mutations and child funding stayed within the original 1.2 SUI non-replenishing run allowance; detailed receipts are in `docs/validation/sf246-testnet-preview.json`.

## Method and evidence

- Starting source: `54ac232d`, current main when this audit began. The existing local app preview was older (`0880dbb7`); the website and video preview also had different source revisions. Source and preview freshness must be distinguished.
- Browser-crawled all eight website pages at 1440×1000 and 390×844, collecting text, headings, links, console errors and screenshots. Read the 404 source separately.
- Built the current app with the committed public testnet profile, visited all sixteen routes signed out, then navigated all sixteen with an actual public testnet account in a read-only Wallet Standard harness. The harness contains no private key and refuses signing. It is not Slush acceptance.
- Waited for actual fleet discovery before reviewing connected states. Initial discovery for this fixture-heavy account took roughly 45 seconds; a short screenshot delay would incorrectly classify the pages as empty. This is an observation, not a typical-customer latency benchmark.
- Read the complete written guide and all nine narration scripts. Transcribed all nine complete MP4 audio tracks locally with faster-whisper `tiny.en`, CPU/int8. Normalized script/transcript similarity was 98–100%; ASR occasionally interpreted the brand as “Sweet Fleet.” Inspected representative frames at 25% and 65% of every video. This checks speech presence, content and visual context; it is not a claim of full real-time human listening or every-frame review.
- Reviewed existing presentation and checkout fixtures for states that the live evaluation profile cannot expose. Fixture evidence is kept separate from real network reads.
- Temporary detailed evidence: `/tmp/suifleet-customer-audit/` (`landing.json`, `app-baseline.json`, `app-dwell.json`, `app-spa.json`, screenshots, `video-transcripts.json`, `video-review.jpg` and test logs). Temporary paths are local evidence, not published artifacts.

## Highest-impact findings

| Priority | Customer interpretation / problem | Correction or remaining action |
|---|---|---|
| High | “I funded the budget, so why can’t I register a device?” Capacity, subscription and network balances were not distinguished. | Renamed Device entitlements to **Device capacity** in navigation, page and registration errors; capacity-full guidance points to Plans and billing when checkout is enabled, otherwise the service administrator; explained the separate limit and funding purposes; added the capacity prerequisite to registration and the written guide. |
| High | “Subscriptions do not exist,” despite a Plans and billing page. | Removed the unconditional claim. Capacity guidance now checks whether the configured manifest supports subscription checkout, and directs customers to billing or the service administrator accordingly. No checkout authority was changed. |
| High | “The console is broken/empty.” Default evaluation configuration has unavailable reporting/storage/data/checkout paths. | Configuration errors use customer language instead of package names. **Still open:** publish and verify a complete evaluation configuration. Existing SF-173 / #339 owns hosted evaluation deployment; a copy change does not finish it. |
| High | Installation and documentation links lead to `docs.suifleet.io`, which did not resolve during this audit. | Website onboarding now links to the actual installation document instead of the repository README. **Still open:** publish the curated documentation portal and verify its public domain and all app links. Do not advertise the domain as operational before that check. |
| High | Security reporting directs customers to an unavailable private-reporting channel or an unverified planned mailbox. | Enabled and verified GitHub private vulnerability reporting; website points to its private report form. Public preview questions use issues; the planned email is explicitly unverified. No report or message was sent. |
| Medium | Recorded demos imply the current layout and omit newer capacity/billing prerequisites. | Dated narrated-demo label and layout disclaimer; current written capacity prerequisite; batch enrollment and private-release access identified as optional paths. **Still open:** SF-245 / [#612](https://github.com/tyler-suifleet/suifleet/issues/612) tracks current-UI recordings, a capacity/billing walkthrough and synchronized captions. Existing narration is retained, not represented as a fresh recording. |
| Medium | “Every action costs money” / “registration means installed and online.” | Security copy distinguishes browsing from approved changes. Website lifecycle distinguishes registration, publication, deployment creation, device application and reported health. |
| Medium | Network internals dominate first-use setup. | Installation commands and key/configuration mechanics moved into named integrator disclosures. Download button says connection settings; the actual filename remains `deployment.json`. Technical instructions and links remain available. |
| Medium | “A failed deployment means the device is currently unhealthy” / “healthy means online now.” | Dashboard introduction specifies the latest received report, not a live connection test. Existing deployment result versus health distinctions retained. Missing reports remain unavailable, never fabricated as healthy. |
| Medium | Pricing appears to require a 10,000-device commitment and sends prospects to an internal business plan. | Clarified graduated bands; removed business-plan CTA; optional reporting/storage price table collapsed; preview funding versus proposed subscription distinguished. All canonical amounts and pricing policy remain unchanged. |
| Medium | Access revocation sounds like remote erasure; rollout language presumes specialist knowledge. | Explain that revocation stops new key issuance and cannot erase received files or uninstall software. Define cohorts as batches; explain manual advancement, offline slots and updates already allowed to start. |
| Medium | Privacy wording implies deleting a device erases nothing at all, or guarantees all history forever. | Distinguish mutable/retirable current records from historical transactions retained by network/archive providers; warn against confidential names. Corrected website metadata that falsely said no scripts. |

## Website coverage

| Page | Customer assessment and action |
|---|---|
| `/` | Clear outcome-led headline and illustrative fleet image, labeled EXAMPLE. Preview status visible. “Know what’s running” depends on device reports; it is not independent live inspection. |
| `/how-it-works/` | Rewritten as prerequisites and a device lifecycle. Wallet explained as an approval tool. Sui/Walrus/Seal architecture moved into a disclosure. Platform installer owns partitioning, reboot and recovery; application and OS payloads are both possible with an appropriate installer. |
| `/guide/` | Nine-stage player fits the viewport and offers written task steps and narrated recordings. Optional batch/private workflows should not look mandatory for a one-device pilot. Recordings remain older than current UI; fullscreen is needed for small console text on a phone. |
| `/pricing/` | Proposal qualifier preserved. Clarified bands, optional detail and separate current-preview balances. Commercial availability, actual offers, provider costs and production all-in operation still need verification; the page is not a payment guarantee. |
| `/docs/` | Distinguishes narrated introduction, installation instructions and technical source documents. Removed misleading README onboarding and planned-domain CTA. App documentation domain remains a launch-readiness gap. |
| `/security/` | Clear customer control model before technical key details. Approval and revocation limitations explained. Existing unaudited/testnet/Seal limitations retained; this review does not certify security. |
| `/contact/` | Working preview issue route and enabled private vulnerability reporting. Planned email status explicit. Private account/device secrets must not be posted to public issues. |
| `/privacy/` | Explains public management metadata and independent providers. Not a substitute for legal review or verified production retention practices. |
| `/404.html` | Recovery/navigation page reviewed structurally; not a product capability. |

## App coverage

All routes below were inspected signed out and through connected read-only navigation. Missing live configuration is not substituted with a fabricated success state.

| Route | Purpose in customer language; assessment |
|---|---|
| `/` | Fleet summary and incidents. Updated report-versus-online explanation. Current test profile could not verify all alerts; that remains an explicit unavailable state. |
| `/get-started/` | Connect account, obtain test funds, install/register a device, then publish/deploy. Added official faucet link, capacity prerequisite and plain client terminology. Raw installer steps disclosed separately. |
| `/devices/` | Device inventory, groups, installed-version/report details. Preserve names as primary identifiers and honest missing-report states. Registration is not proof of connectivity. |
| `/enrollment/` | Scale registration using separate device credentials. Integrator commands retained in disclosures; each device must receive only its own credential. Not required for a one-device pilot. |
| `/components/` | Reusable library of software versions. Clarified publish-once versus deploy-to-devices distinction. Engineering install/run instructions still need an integrator; not every customer can author them unaided. |
| `/firmware/` | Publish software and installation instructions, then select recipients. Replaced artifact/recipe jargon in introductory copy. Uploading or creating a deployment is not proof a target applied it. |
| `/deployments/` | Find devices/groups and inspect successful/failed updates. Existing by-device presentation is substantially clearer than raw deployment IDs; retain deeper technical evidence separately. |
| `/rollouts/` | Start with a batch and approve expansion after reports. Defined batches/cohorts and manual advancement; retained safety constraints. |
| `/access/` | Choose who can decrypt private software, separately from fleet-management authority. Simplified initial text while preserving key caching/revocation limits. |
| `/services/` | Optional software services running on devices, not cloud service subscriptions. Reporting must be configured; no services reported is not proof of an offline device. |
| `/data/` | Device files/logs and controlled sharing. Simplified introduction; current default profile lacked the full data/storage configuration. A live customer end-to-end demonstration remains necessary. |
| `/handover/` | Transfer selected management authority to another account. Current UI distinguishes organization access from resource ownership. Raw destination addresses remain necessary for verification until a verified contact/identity layer exists. |
| `/entitlements/` | Organization device capacity and administrator limit tools. Renamed customer-facing title/navigation; clarified that funds and capacity are different. Registry administrator permissions remain unchanged. |
| `/settings/` | Reporting/update/data cadence and optional security controls. Existing tabs improved readability. Saving settings records choices; it does not deposit funds. Daily reporting is not real-time monitoring. |
| `/billing/` | Available subscription offers, prepayment and receipts. Current profile lacks checkout configuration; actual payment logic separately regression-tested. Tier maximums, prepaid balances and settlement remain concepts needing a fresh narrated walkthrough. No real payment was made. |
| `/budget/` | Funds for device activity and storage, distinct from subscription capacity. Replaced internal missing-package errors with actionable workspace-configuration guidance. |

Connected routes now retain their page title and explain fleet-access verification while waiting for selection. Protected page content still waits for verified fleet selection; this is not an authorization bypass.

## Video coverage and gaps

| Video | Approx. length | Customer lesson / caveat |
|---|---:|---|
| Sign in | 66s | Wallet is an approval tool; demonstrates a scripted wallet, not a real Slush approval. |
| Fund budget | 59s | Device activity and storage balances; newer subscription capacity is a separate prerequisite. |
| Register device | 64s | Public address and registration; installer and device capacity must already be ready. |
| Enroll batch | 75s | Optional scalable enrollment; keep credentials separate and private. |
| Publish version | 131s | Software plus install/run instructions; publishing does not install it. Longest and most technical clip. |
| Deploy group | 80s | Group membership then a deployment; applied result must be checked separately. |
| Staged rollout | 84s | Batches and health checks; requires appropriate reporting and operator advancement. |
| Private access | 49s | Private-file recipients, not administrator access; configure before publishing private software. |
| Fleet settings | 65s | Reporting cadence and cost choices; saving does not fund the budget. |

The existing female narration explains purpose and actions coherently and cursor emphasis is present. Two-frame inspection and ASR are insufficient to certify perfect action/speech alignment throughout. Native caption tracks are absent: on-video instructional captions and written guide steps do not provide a complete synchronized narration transcript. SF-245 / #612 tracks native captions and current-UI recordings before claiming accessible, fully current video onboarding.

## Customer journey required before a paid pilot

1. Prospect can reach the public website, app and installation documentation through verified domains.
2. Explain supported device/platform prerequisites before asking for an account approval.
3. Customer connects, creates/selects a fleet and sees whether device capacity is enabled; a clear path obtains capacity without guessing who an administrator is.
4. Separate subscription payment from network deposits. Show actual enabled offers and testnet funding instructions, not production-like controls backed by missing configuration.
5. Install client, register one device, receive its first report and deploy a harmless sample component.
6. Demonstrate success, a deliberate failure with readable diagnostics, and recovery; distinguish update history from current report freshness.
7. Only then introduce groups, bulk enrollment, private releases, staged rollouts and optional device data.

Production provider credentials, paid checkout, public documentation publishing, human wallet acceptance and Pi acceptance remain separately owned work. This audit creates no evidence for them.

## Verification

Commands and final results are recorded in the SF-244 PR. Core checks cover frontend typecheck/build, all-route desktop/mobile presentation, customer-fleet authorization/selection, device setup links/commands, subscription checkout regressions, pricing generation consistency, website checks and guide viewport navigation. No customer signing, mainnet mutation, physical programming or production payment was performed.


### Checks performed on the audit candidate

- `corepack pnpm@11.0.8 --dir dapp typecheck` — passed.
- `corepack pnpm@11.0.8 --dir dapp build` with committed public testnet configuration and local documentation URL — passed; 16 routes exported.
- `node scripts/test_device_client_links.cjs` — passed all public help links, both setup variants, retained CLI commands and connection-settings download.
- `node scripts/test_subscription_checkout.cjs` — passed pricing, payment-link, effects/event and wallet-hook regression fixtures; no real payment.
- `node scripts/test_customer_fleets_browser.cjs` — passed account/network switching, revocation, errors, delayed reads and create-fleet fixtures after updating the expected customer-facing gate label.
- `node scripts/test_dapp_presentation_browser.cjs` — passed 16 routes, desktop/mobile, empty/loading/error/populated fixtures, navigation and overflow.
- `node scripts/test_hydration.cjs` — passed every route signed out and with a reconnected fixture wallet.
- `python3 -m unittest discover -s site/tests -q` — 41 passed.
- `python3 -m unittest discover -s scripts/tests -p test_subscription_pricing.py -q` — 27 passed; canonical pricing amounts unchanged.
- `python3 site/build.py --out /tmp/suifleet-customer-audit/site-final` and `python3 site/check.py /tmp/suifleet-customer-audit/site-final --require-videos` — passed.
- `node site/tests/guide_browser.cjs` — all 68 written instructions reachable across seven desktop/mobile/landscape viewports; no page/text overflow; keyboard navigation, history/deep links, blocked-JavaScript fallback, actual recording playback and missing-media fallback passed.
- Curated documentation build and internal links — passed using hash-pinned documentation dependencies.

Browser checks used local Chromium via `CHROME_EXECUTABLE`. Temporary screenshot output from the fleet fixture was excluded from the change.

### Local preview delivery

Updated immutable local app and site artifacts, retaining all nine narrated videos. Enabled a leased, persistent local documentation preview so help links can be exercised without the unresolved public documentation domain. Local services start with the WSL user session; this is not public hosting.

- Website: `http://172.27.90.223:3004/`
- Guide: `http://172.27.90.223:3004/guide/`
- App: `http://172.27.90.223:3000/`
- Device installation help: `http://172.27.90.223:3005/device-client/`

The app/site preview metadata identifies the source main commit and unmerged audit candidate. Local links are rewritten only in deployment artifacts, not into production source defaults. WSL addresses may change after restart.
