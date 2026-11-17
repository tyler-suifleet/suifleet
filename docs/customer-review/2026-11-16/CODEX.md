# Codex customer experience review

Reviewed 16 November 2026 against main `b9bb2ef6210624d732ab2de2075ea1826e5300c1` and the public SuiFleet website, console and documentation. Reviewer: Codex, GPT-6. Persona: a Linux/device product team with no blockchain knowledge and limited Greengrass familiarity.

The product’s purpose and platform boundaries are much clearer than its onboarding and commercial model. A customer can understand “send software to my devices,” but cannot reliably progress from that understanding to an independently running first device. The biggest gaps are setup ordering, customer documentation, the installation handoff and the distinction between subscription capacity and network funds. These are concrete problems rather than a request to add more blockchain explanations everywhere.

## Evidence and coverage

- Independently visited 46 distinct successful public HTML routes: 8 marketing pages, the documentation home plus all 21 curated docs, and all 16 console routes. Captured desktop and 390px mobile screenshots, page text, headings, links and browser errors. The generated crawl also visited an image and an artificial double-slash URL; those are excluded from coverage and findings.
- Inspected all 16 app route sources and their major setup, permission, wallet, publishing, reporting, budgeting and transfer components. Some richer settings sections and transaction errors were inspected in source rather than activated through a wallet.
- Connected a deliberately non-signing Wallet Standard test account in a fresh Chrome context to reproduce connected-without-fleet behavior. This test wallet has no private key and refuses every signing method.
- Separately used the already-public guide demo account address with the same non-signing test wallet for read-only chain queries. Selected its existing demo fleet and browsed 15 non-dashboard app routes. This does not establish control of that account, successful authentication by Slush, or ability to mutate its fleet. The dashboard was checked signed out and in source.
- Read all ten written lessons and their complete narrated cue records; compared actions with current UI/source. Sampled two visual positions in every published video. All ten loaded metadata and rendered sampled frames without media errors.
- Ran the existing live captions acceptance against `https://suifleet.io`: every lesson passed captions disabled by default, enable/disable, complete narration cues, media version and responsive checks at widths 390 and 1440. The initial paused-player sampling did not load caption cues; that was a test-method limitation, not a published-caption defect.
- Reviewed the complete overview narration and its player configuration. Pronunciation and a complete auditory/visual watch of every second were not independently assessed.
- Opened the actual Slush connection handoff without signing in. The separate `my.slush.app` popup returned HTTP-style “451 … not available in your region” in this audit environment. This establishes an observed limitation here, not that Slush fails for every customer or region. The old malicious-site warning could not be retested beyond that point.
- Local previews on ports 3000/3004/3005 respond, but the published sites are the primary customer evidence. Local marketing still has evaluation copy that differs from the published production-oriented marketing configuration.

Screenshots, scripts and complete raw captures are retained at `/tmp/suifleet-customer-audit-20261116/codex/` on the review machine. They contain no private keys, login credentials or signing material. The evidence summary committed alongside this report retains counts and actual command outcomes.

## Findings

### C01 — First-fleet setup hides the help needed to complete it

**P1; browser reproduction plus source.** Connect a fresh account and open `/get-started/`. `SignedOutGate` wraps connected users in `FleetScope`, while allowing only Device capacity through without a fleet. The page becomes “Choose a fleet to continue,” with “Create default fleet” above it. The account funding help and faucet link in `FirstRun` are no longer visible. A customer who does not know how to obtain approval fees cannot create the fleet needed to reveal those instructions.

`FleetSwitcher` also creates “My customer” without first asking for an organization name or explaining the imminent paid approval. New organizations have zero device capacity by design; that requirement needs a guided, actionable next step rather than an unexplained limit error. Keep zero-capacity authority intact. Let account setup render before fleet selection while keeping other fleet data/actions isolated.

Sources: `dapp/src/app/layout.tsx`, `hooks/useFleetSelection.tsx`, `components/FirstRun.tsx`, `components/FleetSwitcher.tsx`, `contracts/sources/customer.move`.

### C02 — Sign-in assumes knowledge and availability the customer may not have

**P1; live browser and source.** The main CTA is “Connect Wallet,” while the page asks users to “Sign in.” The live build offers Slush and installed extensions, and says Google is not configured. The guide still presents Google as one of the choices. An unavailable Google integration is operator configuration, not a customer repair task. Slush’s regional refusal in this environment makes an understandable fallback particularly important.

Explain, before the provider handoff, that this is the secure account/approval app and what connecting does. Use consistent vocabulary, show available choices only, and distinguish connection cancellation from a submitted operation. Do not promise ordinary password recovery or hide the actual signing authority.

### C03 — Public docs send customers into engineering history

**P1; live text and source.** `https://suifleet.io/docs/` says “The public documentation domain is not live yet,” even though `https://docs.suifleet.io/` works. The hosted docs contain internal ticket IDs, “Recommendations for Codex,” historical test evidence and developer-specific filesystem paths. These belong in reference/history, not in the operator’s main procedure.

Examples include the enrollment page opening with Move CLI calls, the component page describing a feature ticket instead of giving publishing steps, and wallet help leading with Enoki build configuration that a hosted-console customer cannot change. The installation guide references “Download deployment.json,” while the actual button is “Download connection settings.” Explain what to install, what to click, how to recognize success and how to recover before exposing implementation details.

### C04 — The first-device installation promise has an incomplete handoff

**P1; public docs/source, no installation attempted.** There is no published GitHub release listed at review time. Linux evaluation starts with a repository build; public docs explicitly say prebuilt downloads and a standalone indexed Yocto layer are not published. Address registration then requires finding and writing device object/capability identifiers into configuration. This can be reasonable for an integrator, but it is a poor unqualified “start with one device” experience.

Provide an honest platform selection and ready/not-yet-available checklist. Keep prototype/source-build instructions distinct from a supported release download; never invent an installer URL or bypass signature verification. Release-signing/publication acceptance remains separately tracked.

### C05 — The pinned Yocto build and current profile are incompatible

**P1; independently verified source mismatch.** `suifleet-edge/meta-suifleet/recipes-edge/suifleet/suifleet_1.0.bb` pins `SRCREV=2d231cad…` and `SUIFLEET_CRATE_VERSION=0.2.1`. `profiles/seal-testnet/deployment.json` requires edge client `[0.2.3,0.3.0)`. `suifleet-edge/src/config.rs` rejects a client outside that range. This is an actual compatibility issue, not a copy preference. Update the source pin, crate include and lock digest together, rebuild the relevant recipe/profile and verify the produced client accepts the intended manifest. Do not weaken the manifest check or call a rebuilt recipe a completed physical Pi test.

### C06 — There are two shipping paths without a clear choice

**P1; read-only connected browser and source.** Components is a version library. Its deploy action opens `/firmware?componentVersion=…`. The separate Deploy component sidebar opens a direct upload/install/configuration flow. The latter immediately exposes Artifact, Recipe, JSON pointer overlays, WAL storage terms and optional configurations; all three stage cards remain visible even when later cards are unavailable.

Make library-first deployment the obvious normal path. Label the direct-file workflow as an explicit alternative for advanced/platform use. Explain software file, installation commands and destination in customer terms. Move JSON overlays, file-store sponsorship and transaction internals into technical details without removing their functionality.

### C07 — Guide order and desired-version instructions contradict the product

**P1; all lesson transcripts and current source compared.** Publishing uses Confidential delivery by default, requiring an access policy, but policy creation appears at stage 9 after publishing at stage 6. “You can complete this earlier” does not make that sequence intuitive.

The written deploy-group action says library versions are one-shot and require the ad-hoc flow for desired state. Current `buildCatalogDeploymentTx` accepts `desiredTargets`, the default group checkbox offers desired state, and the narration describes that behavior correctly. Update the written instruction. Distinguish the normal one-device path from optional batch enrollment and staged rollout; explain explicit per-batch operator approval accurately.

### C08 — Pricing has too many competing answers to “what will I pay?”

**P1; live pages and source.** The public page mixes per-device USD bands, monthly minimums, at-cost/design-partner arrangements, an all-in subscription proposal, customer-funded rates, network tokens, storage charges and renewal hedging. The app separately requires capacity, approval fees, device operating funds and storage funding. Several names refer to the same balance.

Lead with unchanged approved commercial terms and a small worked example, then explain included resources and optional/customer-funded arrangements. Keep network mechanics in details. Do not manufacture prices or imply all-in funding is implemented when the app still requires separate deposits. The demo checkout must remain clearly identified inside the app; do not move testnet jargon onto the marketing site, contrary to the owner’s explicit instruction.

### C09 — The contact path undermines the pilot invitation

**P1; live pages.** The contact page advertises `hello@suifleet.io` but says mailbox delivery is unverified and directs customers to public GitHub issues. The privacy page uses the same email without that caveat. A prospective manufacturer should not need a GitHub account or disclose a sales enquiry publicly.

Use one consistent private pilot/contact route and verify it before claiming reliable delivery. This audit did not send email or inspect an inbox. Mailbox/provider acceptance must remain explicit if access is unavailable.

### C10 — The app has too much navigation for a first-time task

**P2; desktop/mobile browser.** Sixteen destinations are shown before a customer has a fleet. Mobile turns these into a horizontal strip with only a few visible labels and no clear grouped menu. Administration is both a section name and a page name; that page is not a general settings destination. Capacity is separate from billing, while software publishing is separate from a second upload form.

Use task-oriented groups and a discoverable mobile menu. Explain each empty page and its next step. A read-only sample experience is worth considering, but no synthetic fleet should be mistaken for real customer data or successful deployment.

### C11 — Troubleshooting still exposes internals or vague failure buckets

**P1; source inspection and read-only rendered copy.** Device/deployment actions use raw `alert()` messages, wallet/SDK/Move text can reach customers, archive errors combine unrelated identity/policy/network/expiry causes, and reporting/storage settings can show several separate pending indicators. A permanent “Data status · history pruned by this connection” badge still needs a customer explanation and a route to more detail.

Give short, actionable messages tied to the operation, preserve diagnostic details, and keep cancellation, executed failure and unknown confirmation distinct. Never encourage a second submission just because confirmation is delayed. Loading observations during read-only navigation were transient; they are not evidence that those services never finish.

### C12 — Logging and storage prerequisites need a usable route

**P1; source and connected browser.** Logs are off by default, and changing archive cadence explicitly does not enable them. There is no clear console-to-device activation handoff explaining how to enable logging. Customers also face WAL, renewal-per-blob amounts, encoded byte allowances and storage epochs.

Explain what is collected by default, what must be enabled on the client, what changes cost and where results appear. Use human units and actionable setup links while preserving the actual storage/retention contract constraints.

### C13 — Irreversible actions need better pre-approval summaries

**P1; source inspection.** The enrollment “only copy” warning follows the download button; a lost-file revoke/reissue procedure is not connected to that warning. The legacy handover Transfer/Finish path lacks the same human summary/confirmation used for destructive device/group operations. The current customer-managed Administration page instead uses organization account grants/revocations and cross-fleet device moves; device moves already confirm, while grants/revocations would benefit from a clear recipient/effect summary. Wallet approval alone should not be the customer’s first clear view of who and what changes.

Present credential handling before download, explain safe recovery for unused claims, and show a recipient/resource/effect summary before irreversible transfer. Preserve recipient acceptance, contract checks, signing authority and all uncertainty safeguards.

## What should stay

The landing page now explains software delivery clearly and looks coherent on mobile. The installation/platform boundary is correct: SuiFleet does not choose SWUpdate/RAUC policy, partitions or reboots. The difference between a deployment request, installation result and current device health is usually stated honestly. Private delivery defaults to encryption. Published captions are optional and function correctly. Settings already use much better grouping than the raw engineering docs.

Do not “simplify” by hiding security-relevant approval details, claiming email/password recovery, making proprietary files public by default, bypassing client compatibility checks or pretending a scripted wallet is an extension-wallet test.

## Acceptance limits

No real wallet was authenticated or signed. No account funds were spent by the browser review, no contract state was changed by it, no private key was loaded, and no physical device or production release was tested. Slush reputation warnings, its actual authenticated consent screen, real wrong-network/rejection behavior, device installation/reboot, and release custody remain separately tracked acceptance. There is no claim that the above fixes have already shipped.
