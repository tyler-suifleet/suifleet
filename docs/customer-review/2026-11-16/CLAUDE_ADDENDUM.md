# SuiFleet customer audit: addendum

**Reviewer:** Claude Opus 5.5 (`claude-opus-5-5`). The four delegated sub-reviews also ran as Claude, read-only.
**Checkout:** `b9bb2ef6`.
**Previous report:** my first report from earlier in this session (I didn't read Codex's report). This addendum completes its coverage and corrects it.

**How this round worked:**
- I didn't recrawl. Docs content comes from the browser text captured in `/tmp/suifleet-customer-audit-20261116/claude/text/`.
- Four sub-reviewers read the remaining pages and the console source in full. Their reports are saved at `/tmp/suifleet-customer-audit-20261116/agent-*-report.txt`; I read all four in one call.
- I re-checked the key claims myself; those are marked **[verified]**.
- Nothing was run with a real wallet or on chain.

## Owner constraint (corrects my first report)

My first report's P1-1 asked for a testnet banner on the marketing site. **I withdraw that.** It conflicts with your direction that the marketing site should be production-oriented with no testnet jargon.

What should change instead:
- **Marketing site:** neutral CTAs such as "Try the console" or "Start an evaluation" and "Talk to us about a pilot". No blockchain or testnet wording.
- **Console:** the app itself should explain the evaluation environment. Today that is missing or inconsistent (see A-9).

The same applies to my first-report P1-4 (pricing jargon). Simplify pricing toward an all-in commercial price; don't add test-network explanations to it.

## Completed coverage

Evidence methods:
- **L** = live browser crawl from the earlier pass
- **T** = full read of captured live text
- **C** = code-only source inspection
- **N** = narration and caption records
- **U** = not tested with a real wallet

| Area | Items | Method |
|---|---|---|
| Marketing | 10 pages (home, how-it-works, pricing, guide, docs, security, contact, privacy, film-preview, 404) | L + T, all read in full |
| Docs (operators) | install, device-client, enrollment, components, greengrass, platform-installers, wallet | T in full + C cross-checks |
| Docs (integrators) | yocto, pi-image, macos, releases, deployment-manifest, component-ipc, key-isolation | T in full + C (recipe, unit file, Cargo, profiles, trust.toml) |
| Docs (administrators) | access, access-policies, seal, fleet-settings, budget, pricing, handover, docs home | T in full + C (price table, settings, budget, handover, access) |
| Console, signed out | all 16 routes | L |
| Console, signed in | all 16 `dapp/src/app/*/page.tsx` routes plus their main components (FirstRun, FleetSwitcher, FleetScope gate, ComponentPublisher, DeliveryFields, firmware flow, DeviceEntitlements, billing, enrollment, data/StreamRetention, FleetSettings, FleetBudget, handover/OrganizationSecurity/CustomerAccess, useFleetTransaction) | **C only** |
| Wallet states: connect, cancel, reject, wrong network, insufficient funds, pending, uncertain | | **C only. U** |
| Guide stages | 10/10 actions, prerequisites and results compared label by label with source | C (34 of 36 labels found exactly; the other 2 differ only slightly) |
| Guide narration | 10/10 cue transcripts | N, compared with source |
| Overview and film-preview narration | full `.vtt` transcripts | N |

## New findings, in priority order

### P0: blocks a self-serve evaluator

**A-1. There is no installable device client.** [verified]
- device-client page: "No prebuilt download is published yet… Ask your fleet operator".
- install page: build with "Git, Docker Engine/Compose and GNU Make… `make dev` / `make edge-build`".
- macOS page: `cargo build` from source.
- releases page: describes signed bundles, but `release/trust.toml` has `keys = []` and says "The release workflow has not run".
- **Effect:** the marketing promise "Start with one device… send your first update" can't be met without building from source. This is the biggest gap between marketing and product.

**A-2. Get started is unreachable for a signed-in user with no fleet.** [verified, code-only]
- `app/layout.tsx:111` wraps signed-in pages in `FleetScope`, and only `/entitlements` is allowed without a fleet.
- A new user on `/get-started` sees "Choose a fleet to continue…" (`useFleetSelection.tsx:68`) instead of the setup guide.
- **Effect:** the funding help and faucet link (`FirstRun.tsx:171-172`) are hidden exactly when the user needs fees to create a fleet.
- Fleet creation is an unexplained underlined "Create default fleet" button (`FleetSwitcher.tsx:26`). It hard-codes the organization name "My customer" and shows raw `e.message` errors.

**A-3. Every new organization starts with a device limit of 0.** [verified]
- Source: `contracts/sources/customer.move:117` (`device_limit: SMALL_0`).
- Registration is disabled until capacity is bought or granted. The message reads: "Device limit reached: 0 active devices, limit 0… Open Device capacity…" (`lib/customer-entitlements.ts:13`).
- The live testnet billing page does show two demonstration offers (100 devices, 0.01 USDC or SUI), so self-serve capacity *appears* possible. **U:** that purchase wasn't tested.
- If no offers are configured, the user is told to "Ask your service administrator"; a self-serve evaluator has none.

### P1: misleads or blocks a task

**A-4. Guide stage 7 contradicts current deploy behaviour.** [verified]
- `site/guide/steps.json` (deploy-group) says: "A catalog version deploys once; to make it a group's desired version, use the ad-hoc Deploy component flow."
- Current source: catalog deploys pass a desired-state flag (`app/firmware/page.tsx:144`, `lib/catalog.ts:22,29`). The checkbox "Make this the groups' desired version…" is on by default (`firmware/page.tsx:90,423-425`).
- The stage 7 narration ("Desired state keeps this version requested for its members") matches the code; the written step doesn't.

**A-5. The guide's stage order breaks the default publish path.** [verified]
- Stage 6 (Publish) uses Confidential delivery by default (`DeliveryFields.tsx:8`), which needs an access policy.
- The policy isn't created until stage 9 (Private-release access).
- Following the guide in order, a customer either hits "You administer no access policies yet" or "Choose an access policy you administer for confidential delivery" (`lib/delivery.ts:29`). The alternative is to copy the video and switch to Public.
- The stage 9 summary does say "You can complete this step earlier", but the order is still wrong.

**A-6. Components and Deploy component are two different ways to ship software, with no explanation.**
- `/components` publishes into a library. "Deploy this version" goes to `/firmware?componentVersion=`.
- Opened from the sidebar ("Deploy component"), `/firmware` is a separate upload-and-recipe flow with its own delivery and storage choices.
- Get started step 4 says "Publish a version in Components…" but its button "Deploy a component" links to `/firmware` (`FirstRun.tsx:251-253`) [verified].

**A-7. The docs use the wrong console button name.** [verified]
- device-client page: "The console's **Download deployment.json** button".
- The actual button reads "Download connection settings" (`components/DeviceClientHelp.tsx:27`), and it renders only when the manifest environment variable is set.

**A-8. The Yocto/Pi recipe builds a client version the current network profile rejects.** [verified, code-only]
- `meta-suifleet/recipes-edge/suifleet/suifleet_1.0.bb:20,29` pins SRCREV `2d231cad`, crate version 0.2.1.
- `profiles/seal-testnet/deployment.json:169-171` requires edge ≥ 0.2.3. Main is 0.2.3.
- The daemon refuses an out-of-range version (`config.rs:470-478`, per the sub-review).
- I couldn't fetch the live app manifest (`app.suifleet.io/deployment.json` returned 404), so this is confirmed against the repo profile only.

**A-9. The console doesn't explain that it is an evaluation environment.**
- The only signals are a "Testnet" pill, the billing notice (`billing/page.tsx:93`), the one-liner "Testnet uses free test tokens." (`PilotReadiness.tsx:28`), and the faucet text in a collapsed panel that A-2 makes unreachable.
- The Network budget page's "Need funds in your account first?" offers card purchase and no faucet (`budget/page.tsx:22`).
- Nothing says what happens to fleets, devices or data at commercial launch.
- **Recommendation:** add one consistent evaluation notice to the console only (for example in the top bar or Get started) covering: no monetary value, how to get test funds, and the path from evaluation to production.

**A-10. Internal engineering content is published on docs.suifleet.io.** [verified samples]
- **Ticket IDs:**
  - Counts per page: greengrass 46, budget 25, yocto 21, component-ipc 10, key-isolation 9.
  - Page titles: "(SF-016)", "(SF-021, SF-100)", "(SF-088, SF-101)", "(SF-130)", "(SF-083)", "(SF-089)", "(SF-022)".
- **"Recommendations for Codex"** section: docs_greengrass.txt:257.
- **Developer home paths:** 16 `/home/tyler/...` paths on the Yocto page. The Pi page tells readers to use `\\wsl.localhost\<distribution>\home\tyler\dev\codex\yocto-sf120\artifacts\`, a private developer directory.
- **Test-only material:** CI and merge-gate notes ("exact-head CI/testnet status remain merge gates"), make/pytest commands, and testnet resource-lock procedures.
- **Effect:** this undermines the production-oriented positioning far more than any banner question.

**A-11. Docs contradict the product or each other.**
- **Handover vs Seal and budget:** the handover page says "No Seal access implementation exists in this baseline (SF-022)" and "No SF-083 budget policy API exists here" [verified]. Both features ship.
- **Greengrass page contradicts itself:**
  - "only the SF-037 recipe translator is implemented" and "Not available natively yet… MQTT/IoT Core, shadows, streams"
  - versus later sections listing pub/sub, shadows and streams as implemented.
- **Service fee:**
  - Budget page: "Service fee: open business decision… No SuiFleet fee is charged unless the user decides otherwise" [verified].
  - Marketing pricing and console billing both describe a paid subscription.
- **Who pays network costs** has four different answers:
  - Marketing: "all-in subscription… customers need not hold SUI or WAL".
  - Docs pricing: "The owner prepays… SUI".
  - Console: "Add SUI to your fleet budget".
  - Billing: "Device network fees… are paid separately".
- **Fleet settings deposit:** the docs say the settings page "collects the prepaid deposit"; the console says "Saving settings does not buy a subscription or add money to your budget" (`FleetSettings.tsx:263`).
- **Yocto service unit:** the Yocto page says the unit "has no privileged pre-start commands", but the unit has `ExecStartPre=+/usr/libexec/suifleet-provision` [verified]. The key-isolation page is correct.
- **Stale versions:** the components page says "frontend is version 0.1.0 and daemon 0.2.0"; the repo is at 0.1.1 and 0.2.3.

**A-12. "(not yet approved)" pricing label in non-testnet builds.** [verified, code-only]
- `lib/fleet-price-table.json:8` has source "…pricing proposal, 2026-10-23 (not yet approved)", which `billing/page.tsx:96` renders.
- The live testnet build shows "Testnet demonstration tariff only" instead.
- Any non-testnet build would show customers "(not yet approved)" next to prices the marketing site presents as real.

**A-13. Raw errors are shown to customers, often in browser popups.** Code-only.
- There is no shared error translator.
- About 13 `alert()` calls in `devices/page.tsx` [verified count], plus others in deployments and firmware (e.g. `Failed: ${err.message}`, "Deployment failed: …", "Upload failed: …").
- Untranslated strings include "Wallet network does not match the deployment manifest" and the chain's `status.error.message`.
- A wallet rejection is detected (code 4001) but the user still sees the raw text.

**A-14. Enrollment credential download.** Code-only.
- "Download credentials" downloads immediately. The "only copy of the claim keys" warning sits *below* the button (`enrollment/page.tsx:391,399`).
- If the archive is lost, nothing points to "Revoke unused claims" or "Remove unused reservations".
- The page still assumes `scripts/claims.py` (repo-only) for offline key generation.

**A-15. Log uploads can't be turned on from the console.** Code-only.
- Logs are off by default, and settings text says "This does not turn logging on" (`FleetSettings.tsx:161`).
- No console control turns them on, and no docs page says how.
- Marketing advertises "Includes 10 MB written per device per month, kept for 42 days".

**A-16. The handover step has no confirmation, and funds can't be handed over.** Code-only.
- "Transfer next N resources" and "Finish transfer" sign without a `confirm()` dialog (`handover/page.tsx:126-127`). Device and group deletion do confirm.
- Supported resource types are devices, groups, deployments, role revocation and enrollment close (`lib/fleet-handover.ts:20`).
- The budget (which holds funds) and the access-policy admin key are not transferable, and the docs don't explain how to move them.

**A-17. The wallet docs are for self-hosters.**
- Most of the page is Enoki/`.env.local` rebuild setup.
- It never explains the live message "Google sign-in is not configured for this build."
- It uses names the console doesn't: "add gas" for Add funds, "Settings → Organization security" for Fleet settings, and "Not available yet" for "Unavailable".

### P2

- **Jargon shown in the signed-in console** (code-only):
  - "Walrus epochs" (1–53), "Storage uses WAL", "Maximum renewal WAL per blob"
  - MIST/FROST in details panels, "Key epoch", "Object ID" table columns, "Device SUI address" with a "0x…" placeholder
  - "publisher catalog on Sui… container for your immutable releases"
  - raw transaction digests shown in plain view on billing and the publisher
- **Fleet settings field names:** docs use raw keys (`check_interval_ms`, `archive_retention_epochs`); the console labels are "Update check", "Health reports", "Log retention". Add-on prices in `fleet-price-table.json` (for example 5-minute checks $0.05, hourly health $0.12) appear on neither the marketing nor the docs pricing pages.
- **Plans missing from the console:** the Managed plan, at-cost tier and design-partner terms on the marketing site have no console equivalent.
- **Activation fee:** "Activation is separately quoted" in billing, versus $0.10 per device on the marketing site and in settings.
- **Missing walkthroughs:**
  - The components docs page has no step-by-step publish procedure, though install sends customers there.
  - The Greengrass page has no console migration walkthrough, though its field-mapping table is useful.
- **Address registration needs a CLI:** the docs say to find the DeviceCap ID "in a Sui explorer, or `sui client objects`".
- **Pi image security:** the image grants passwordless sudo. It is labelled as a development image, but there is no production image path.
- **Label drift:** "Register core device by address" (modal) vs "Register by address"; "device places" vs "Device capacity"; MiB vs MB for the 10 MB allowance.

## Guide and narration vs source

- **Labels:** 34 of 36 guide labels match source exactly. "Run pending transactions" renders as "Run N pending transactions"; "reporting interval" is split into "Update check" and "Health reports". Both are minor.
- **Substantive mismatches:** A-4 (stage 7 desired state) and A-5 (stage order).
- **Narration** for all 10 stages is otherwise consistent with current behaviour and careful about claims ("does not establish that a long-running service is healthy"; "Pause stops new admissions. It does not undo…").
- **Stage 1 vs live build:** stage 1 still offers Google, which the live build has disabled.
- **Overview and film-preview narration:** "Sui saves your approval… Walrus stores your software files" names the underlying technology without explaining it. Under the owner constraint, marketing narration should describe outcomes ("a shared, tamper-evident record"; "encrypted file storage") rather than adding more chain names.

## Corrections to my first report

1. **Withdrawn:** P1-1 (marketing testnet banner). Replaced by neutral marketing CTAs plus A-9 in the console.
2. **First report P1-5 overstated the signed-out problem and missed the worse signed-in one:** signed in without a fleet, Get started is hidden entirely (A-2).
3. **First report P2-4 said every label the guide cites still exists.** That is true at the label level, but stage 7's behaviour description is outdated (A-4) and the stage order is broken (A-5).
4. **The "Docs not live" finding (first report P1-2) still stands.** The bigger docs problem is that docs.suifleet.io publishes internal engineering material (A-10) and contradicts itself (A-11).
5. **First report P1-3 (email) and P1-6 (terminology) still stand.** P1-6 extends to "who pays" (A-11).
6. **New:** my first report missed the no-download blocker (A-1) and the zero starting device limit (A-3).

## Still untested

- No real wallet was used: no fleet creation, funding, capacity purchase, registration, publishing, deployment, rollout, access grant, settings save, handover or data access.
- Every signed-in and wallet-state statement (cancel, reject, wrong network, insufficient funds, pending, uncertain) comes from reading source code. A mocked transaction proves nothing about a real wallet.
- No device client was built or installed; no Yocto, Pi or macOS work and no chain transactions.
- The A-8 version mismatch is confirmed against the repo profile, not the live app manifest.
- Guide videos were assessed through narration records, not watched frame by frame.
