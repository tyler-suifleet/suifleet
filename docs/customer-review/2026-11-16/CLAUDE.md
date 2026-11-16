I've completed the audit. I found no P0 blockers. The biggest customer problems are that the marketing site never mentions the console is a testnet demo, the published contact and documentation information contradicts itself, and the money concepts are confusing for someone with no crypto background.

# SuiFleet customer usability audit (independent)

**Reviewer:** Claude Opus 5.5 (`claude-opus-5-5`), read-only, separate headless Chrome (`/opt/google/chrome/chrome`, Node Playwright).
**Checkout compared:** `b9bb2ef6210624d732ab2de2075ea1826e5300c1` (clean main).
**Evidence:** `/tmp/suifleet-customer-audit-20261116/claude/`. Screenshots are in `shots/` (desktop 1366×900 and mobile 390×844, full page). Extracted page text is in `text/`, and per-page console and network results are in `{site,docs,app}_results.json`. The crawl script is `crawl.cjs`.
**Persona:** a Linux/IoT team evaluating OTA tooling, with no blockchain knowledge and limited AWS Greengrass knowledge.

## Coverage matrix

How each item was checked:
- **L** = live browser crawl (desktop and mobile, screenshot, text, console and network)
- **C** = code or source inspection only
- **N** = narration and caption records read from the repo (`site/media/guide/videos.json`, `.vtt` files); the videos were not watched frame by frame
- **S** = simulated wallet (only inside the recorded guide videos, at older commits)
- **U** = untested with a real wallet

All three public sites (suifleet.io, app.suifleet.io, docs.suifleet.io) returned 200, so I used them rather than the local previews. I confirmed that localhost:3004, :3000 and :3005 respond and have matching titles, but did not crawl them.

| Area | Items | Method | Result |
|---|---|---|---|
| Marketing suifleet.io | `/`, `/how-it-works/`, `/pricing/`, `/guide/`, `/docs/`, `/security/`, `/contact/`, `/privacy/`, `/film-preview/`, 404 | L | No horizontal overflow, no console errors, no images missing alt text. Media requests showed as aborted only because of lazy loading. The 404 page returns a correct 404. |
| Docs docs.suifleet.io | Home plus all 21 pages in `site/docs/pages.json` (install, device-client, enrollment, components, greengrass, platform-installers, wallet, yocto, pi-image, macos, releases, deployment-manifest, component-ipc, key-isolation, access, access-policies, seal, fleet-settings, budget, pricing, handover) | L (full text of home and pricing read; the rest checked structurally only) | All 200, no overflow, no errors |
| Console app.suifleet.io, signed out | All 16 routes in `dapp/src/app`: `/`, get-started, devices, services, deployments, rollouts, components, firmware, data, enrollment, access, handover, entitlements, settings, budget, billing | L | All 200. Aborted requests are Next.js prefetches. 14 of the 16 routes show only a sign-in gate. |
| Console, signed in | Fleet creation, funding, registration, publishing, deploying, rollouts | **U** live. C for transaction error handling (`dapp/src/hooks/useFleetTransaction.ts`) and the first-run faucet copy (`FirstRun.tsx:172`). S only in the guide videos. | Not run with a wallet, per the rules |
| Wallet states: connect, cancel, reject, wrong network, insufficient funds, pending, uncertain | | C only. The code handles rejection (4001 / `UserRejectedRequestError`), wrong network, a network change during signing, insufficient subscription funds, a submitted-but-unconfirmed state ("may have reached the network, refresh before retrying"), and confirmation against an independent node. | **U** with a real wallet |
| Guide stages (`site/guide/steps.json`) | sign-in, capacity-plan, fund, register-device, enroll-batch, publish-version, deploy-group, staged-rollout, access-policy, fleet-settings (10/10) | L (guide page) + C. All 13 UI labels the guide cites exist in current `dapp/src`. | |
| Guide videos and narration | 10/10 records in `videos.json`. Caption tracks are present for each. | N + L (each player has controls, a captions track and a poster; none autoplay) | |
| Overview film | `overview-landscape` / `portrait`, `overview.en.vtt` (104 s) | N + L | |
| Motion sample | `/film-preview/` with `sample.en.vtt` | N + L | |

## What a first-time customer experiences

1. **Home page.** "Your fleet. Under control." makes it clear this is for deploying software to devices, and "SWUpdate, RAUC, or your own commands" reassures embedded teams. But "Sui and Walrus" appear without explanation ("The console and device client use Sui and Walrus directly"), and nothing says the product is a preview or that the console runs on a test network.
2. **How it works.** This is the best explanation on the site: a wallet is "the app you use to sign in and approve changes", and capacity and funds are separate. The overview film's narration is clear and honest about what happens on the device versus the platform. It still says "Sui saves your approval on a shared network" and "Walrus stores your software files" without saying what those are or who runs them.
3. **Pricing.** I see USD per-device bands, then a jump into SUI, WAL, gas float, relay fees, "all-in subscription proposal" and "3× the quote basis". I can't work out what I would actually pay for 50 devices.
4. **Launch app.** I land in a console with a small "Testnet" pill. Every page except Get started and Plans and billing says only "Sign in to continue", so I can't see what the product looks like without first getting a crypto wallet. Get started is well structured (three separate requirements, a four-step tracker, a "You hold this fleet's keys" warning). Plans and billing shows "$0.0001 per device/month", "0.01 USDC" and a raw `0x…` "Treasury payee", none of which match the marketing pricing.
5. **Help.** Clicking Docs takes me to a page saying the docs domain "is not live yet" and sending me to GitHub, even though docs.suifleet.io works. The contact page tells me not to rely on the email address.

## Findings

### P1-1. The marketing site never says the console is a testnet demo
- **Where:** `https://suifleet.io/` (CTAs "Launch app" and "Explore the console"), `/pricing/` and `/how-it-works/`. In source, `site/src/pages/{index,pricing,how-it-works,guide}.html` contain no mention of "testnet".
- **What the customer sees:** The console shows a "Testnet" badge and "Testnet Workspace". Plans and billing says "Testnet checkout demonstration … Use test tokens only. They have no monetary value."
- **Effect:** Someone who arrives from the $0.08/device pricing finds a demo with fake money and no explanation of how to reach production.
- **Repro:** suifleet.io → Launch app → Plans and billing.
- **Recommendation:** Add a preview/testnet banner to the marketing CTAs and the pricing page, explaining that the console is currently a test environment and how pilots move to production.

### P1-2. The site's Docs page contradicts the live docs site
- **Where:** `https://suifleet.io/docs/` (`site/src/pages/docs.html:3`) says: "The public documentation domain is not live yet. The links below open the current written guides on GitHub". It links 20+ GitHub `blob/main` files.
- **Contradiction:** `https://docs.suifleet.io/` is live, and `/guide/` already links to `https://docs.suifleet.io/install/`.
- **Effect:** The main nav sends customers to raw GitHub markdown and says something false about the docs.
- **Recommendation:** Point Docs at docs.suifleet.io and remove the sentence. Pages that only exist on GitHub (STAGED_ROLLOUTS, DASHBOARD_VIEWS, SERVERLESS_PRODUCT, client configuration) should either be published on docs.suifleet.io or marked clearly as GitHub-only.

### P1-3. The contact email is published but disclaimed
- **Where:** `/contact/` says: "Our support address is hello@suifleet.io. Mailbox delivery has not been verified yet, so please use the GitHub channels above until delivery is confirmed."
- **Contradiction:** `/privacy/` gives "Questions about this notice: hello@suifleet.io" with no caveat.
- **Effect:** The only non-public channel for pilots is GitHub, which needs an account and makes issues public. The pricing and design-partner copy says "Contact us", but there is no dependable private way to do that.
- **Recommendation:** Verify the mailbox or remove it everywhere. Provide one private pilot/sales channel.

### P1-4. Pricing is built on crypto terms and reads like an internal proposal
- **Where:** `https://suifleet.io/pricing/`. Quotes: "The all-in subscription proposal funds the fleet network budget, so customers need not hold SUI or WAL"; "SUI pays on-chain transactions, storage deposits and gas float"; "Volume rates below about $0.07 per device assume customer-funded SUI network costs"; "Deployment-specific price overrides may differ from this public proposal"; "renewal is requoted if … at least 3× the quote basis".
- **Effect:** SUI, WAL, gas float, relay fees and Walrus egress are never defined. The word "proposal" makes the prices sound non-binding. A reader can't tell whether $0.08 includes network costs. "At-cost … about $0.01 per device per month" and "below about $0.07" sit side by side without any reconciliation.
- **Recommendation:** Lead with the all-in USD price and say plainly what's included. Move the token mechanics into a "How network costs work" section with a glossary. Remove the "proposal" wording.

### P1-5. The console can't be seen without a wallet, and the sign-in wording doesn't match the button
- **Where:** 14 of 16 routes (for example `https://app.suifleet.io/devices/`) show: "Sign in to continue — Sign in with the wallet control in the top bar". The button is labelled "Connect Wallet" (screenshots `app_*_desk.png`).
- **Effect:** An evaluator with no crypto knowledge has to install or create a Slush wallet before seeing a single product screen. The text says "Sign in", the button says "Connect Wallet", and the guide also says "Connect Wallet". Google sign-in, which the guide promotes, is "not configured for this build" on the live console.
- **Recommendation:** Offer a read-only demo fleet or sample screenshots per page while signed out. Use one verb consistently (for example "Sign in (wallet)"). Make the guide's stage 1 match what the live build actually offers.

### P1-6. Too many names for the money and capacity concepts
Observed, live:
- Get started uses "Account fees", "Device operating funds", "fleet budget" and "network budget".
- The sidebar uses "Network budget", "Device capacity" (route `/entitlements`) and "Plans and billing".
- Pricing uses "fleet network budget", "operating funds" and "network funds".
- Narration uses "operating funds" and "subscription allowance".
- Docs use "Fleet network budget" and "Budget and pricing policy".

On top of that, storage funding is a separate balance.

**Effect:** The product goes to real effort to explain that there are three or four separate balances, but naming each one several ways undoes that.
**Recommendation:** Pick one term for each concept (for example Device capacity / Approval fees / Device network budget / Storage budget) and use it in the site, console, guides and narration.

### P2-1. Some sidebar labels hide what the page does
- `/handover` is labelled "Administration", which is also the name of its section heading. The page itself says "Transfer control of selected devices and resources to another administrator."
- `/firmware` is labelled "Deploy component".
- Capacity appears in two places: "Device capacity" and "Plans and billing".

**Recommendation:** Rename the item to "Fleet handover". Consider folding capacity into Plans and billing.

### P2-2. Blockchain internals shown to customers on Plans and billing
- **Where:** `https://app.suifleet.io/billing/` shows "Treasury payee: 0xeef4c5c3…f24e — Payments use this manifest-pinned address. No SuiNS lookup chooses the recipient."
- **Recommendation:** Show "Paid to: SuiFleet (verified)" and put the address and verification details behind a "details" toggle.

### P2-3. Some raw transaction errors use internal terms (code-only, not tested with a real wallet)
- **Where:** `dapp/src/hooks/useFleetTransaction.ts:39-46,75,99-101`. Examples: "Wallet network does not match the deployment manifest", "Wallet account does not support sui:testnet", "Insufficient funds for subscription deposit; gas also requires SUI".
- I did not confirm whether these strings reach the screen unmodified.
- **Recommendation:** Translate them, for example "Your wallet is on a different network. Switch it to Testnet and try again."

### P2-4. Guide videos were recorded on older console builds
- **Evidence:** `videos.json` dates them 2026-11-05, across console commits ad3da5a9, 3586b750, 77632f67, ac6c608b and 88d23640. Current main is b9bb2ef6.
- **Mitigation already in place:** The guide notes "Demo layout may differ", and every UI label the narration cites still exists in the source.
- **Risk:** Mild, but there is no check that video layouts match the current console.

### P2-5. Production loose ends on public pages
- `/film-preview/` is publicly reachable and described as "Twenty seconds exploring a new visual direction for SuiFleet". It's an internal sample.
- The footer on every page includes "Download transparent logo ↗" and "Fleet management, on your terms · Security and trust", which read like leftover press-kit or internal links.

### P2-6. Mobile console navigation is a horizontal scroller
- **Where:** `app_get-started_mob.png`. The 16 sidebar items become a horizontal tab strip with "Device services" cut off at the edge.
- There is no page overflow, but items beyond the first four are only discoverable by swiping sideways.
- **Recommendation:** Use a menu button or grouped sections on mobile.

### P2-7. Narration and copy name the underlying technology without saying what it is
- **Overview narration:** "Sui saves your approval on a shared network. Walrus stores your software files…".
- **Home page:** "use Sui and Walrus directly".
- A one-line gloss would help, for example "Sui (a public ledger) … Walrus (a decentralized file store)". The security and privacy pages do explain that records are public, which is good.

## Things done well
- **Honest claims throughout:** "A deployment-created message confirms the request, not installation"; "Applied … does not establish that a long-running service is healthy"; "software keys are not hardware attestation"; "No independent third-party audit has been published yet."
- **Clear platform boundary:** your platform owns installation, reboot and rollback, which suits Yocto/RAUC teams.
- **Get started** separates capacity, account fees and device funds and has a clear "Next:" step. After sign-in it links the faucet (`FirstRun.tsx:172`, code-only).
- **Media accessibility:** every video has controls, an English captions track and a poster, with no autoplay. The full narration is available as text, and the voice is disclosed as synthetic.
- **Layout and errors:** no horizontal overflow, no console errors, and no images missing alt text across 46 public pages (desktop and mobile). The 404 page is proper.
- **Privacy:** no cookies or analytics, and the privacy page states clearly that on-chain names and memberships are public.
- **Transaction safety in code:** rejection, network changes during signing and "may have reached the network" uncertainty are all handled, and confirmation comes from an independent node (code-only).

## Limitations and untested acceptance
- **No real wallet was used.** Signed-in console pages, fleet creation, funding, registration, publishing, deploying, rollouts, access, settings, handover, data and device services are unverified live. Cancel, reject, wrong network, insufficient funds, pending and uncertain states were checked in code only.
- **Videos:** the 10 guide videos and the overview were assessed from their narration and caption records plus player markup, not watched visually. Their in-video wallet is a scripted test wallet (simulated) at older commits.
- **Docs depth:** 19 of the 21 docs pages were checked structurally (load, overflow, errors), not read in full for accuracy.
- **Local previews** (3004, 3000, 3005) were only confirmed reachable, not crawled, because the public sites were up.
- **Not done:** device client installation, Pi or Yocto work, Greengrass migration, and any chain transaction. Nothing in this report claims those work.
