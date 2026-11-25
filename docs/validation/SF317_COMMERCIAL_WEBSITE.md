# Commercial website review — SF-317

Reviewed the eight public marketing routes (home, How it works, guide, pricing,
docs, security, contact and privacy) in Chromium, starting with the live site.
The review assumes a device operator unfamiliar with blockchain terminology.

## Findings addressed

- Replaced demo-account and test-plan narration with direct sign-in and capacity
  instructions. Re-encoded the two affected lessons with the existing female
  Kokoro voice and optional captions, using the verified original action footage.
- Removed the guide's demo-layout footer and the written demo-group instruction.
  The rollout instructions describe the saved/paused state and the operator's
  next steps instead of internal acceptance disclaimers.
- Replaced unverified-mailbox and public-fallback language with a clear private
  sales/contact path. No response-time promise was added.
- Clarified that the email provider processes correspondence; no compliance or
  confidentiality guarantee was invented.
- Replaced tentative proposal wording in generated pricing copy. Rates, minimums,
  quote requirements, customer-funded arrangements and renewal thresholds remain
  unchanged. Support commitments still require a written agreement.
- Moved the docs introduction away from internal hardware-acceptance terminology,
  used “Transfer fleet administration”, and clarified the account-approval heading.

## Boundaries preserved

The app retains its network/environment notices. Security still states that no
independent third-party audit has been published. Public metadata, encryption,
revocation and platform-owned installation/reboot responsibilities remain clear.
Illustrative homepage data remains labeled as an example. Recorded guide footage
is historical: refreshed narration does not establish fresh production, wallet,
mainnet or physical-device acceptance. Eight unchanged lesson media sets retain
exact previous hashes; historical narration timing is untouched.

## Verification

Results are recorded below after the final build and browser checks.

- `python3 scripts/subscription_pricing.py --check-site` — passed; generated
  pricing matches the unchanged numeric policy/table.
- `python3 site/build.py --out /tmp/sf317-site-final` — passed, 77 files.
- `python3 -m unittest discover -s site/tests -p 'test_*.py'` — 84 tests passed.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/guide_browser.cjs /tmp/sf317-site-final`
  — passed at seven desktop/mobile sizes: all 80 instructions, overflow, keyboard,
  static fallback, real media errors/retry, native captions matching every lesson,
  playback and inactive-video pause. Captions work at desktop and phone sizes.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/how_it_works_browser.cjs /tmp/sf317-site-final`
  — passed eight viewports including 320×568 and 568×320; all 18 explanations
  remain accessible without document/text overflow.
- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/pricing_browser.cjs /tmp/sf317-site-final`
  — passed desktop/phone worked bills, private contact path, optional details,
  docs links and overflow. Updated an obsolete documentation-link test selector.
- Additional Chromium audit: all eight public pages at 1440×900, 390×844 and
  320×568, no horizontal overflow, script errors or removed internal/demo phrases.
  Visually inspected the phone guide and contact screenshots.
- Two refreshed clips: local Kokoro 0.4.9 `af_heart`, pinned cached model/voice;
  original caption-free source revision `d21a8757`. Sign-in 40.76 seconds and
  capacity 46.44 seconds; complete MP4/WebM decode passed. Render inputs and prior
  manifest retained in `selective_refresh`; eight other media sets verified by hash.
- `git diff --check` — passed. No wallet, chain, app or hardware writes were
  needed for this website-only change.

The initial build correctly rejected a mismatched historical-script digest in
assembly. The manifest now references the actual preserved script snapshot,
retains the separate render-input hashes, and passes the full provenance checks.
No integrity validation was bypassed. Human/mainnet walkthrough capture remains
separate from these historical tutorial edits.

## Mobile landing-page follow-up

The user requested a clearer mobile flow after the first audit. The home page
now leads with a concise product explanation and “How it works”, followed by a
readable illustrative deployment, prepare/deploy/verify steps, platform integration,
security and getting started. Removed repeated links, a redundant platform card,
numbered security lists and recipe terminology. Kept the existing brand and
platform-owned installation/reboot responsibilities.

Added native disclosure navigation for the home page at phone/tablet widths.
It works without JavaScript and keeps all six destinations available. Other
pages retain their existing navigation and guide layout. Home-only responsive
styles improve body copy to at least 15px and keep primary navigation/touch
controls at least 44px high.

Measured at 390px: header 176→81px, workflow section 1494→977px, total page
5013→4364px while increasing body-text size. At 320px: total 5414→4748px.
These are observed results, not brittle pixel limits in the regression test.

Additional verification:

- `CHROME_EXECUTABLE=/usr/bin/google-chrome node site/tests/landing_browser.cjs /tmp/sf317-mobile-site /tmp/sf317-mobile-shots`
  — passed 320, 390, 560, 768, 800, 801 and 1440px. JavaScript disabled;
  keyboard open/close/tab navigation, real Pricing navigation, touch targets,
  menu/document bounds, legible phone body copy and desktop navigation.
- Static site build and all 84 site tests passed after the header/layout changes.
- All 30 subscription-pricing tests passed, preserving the automatic CI repair.
- Shared guide browser checks passed (all 80 instructions, keyboard, captions,
  playback, static fallback and media recovery). How it works passed all eight
  viewport checks. Final copy/footer refinements are scoped to the landing page.
- Visually inspected full phone and desktop renders; no fabricated customer
  metrics, testimonials or production acceptance were added.
