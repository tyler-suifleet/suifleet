# Proportional change workflow

User direction, 2026-11-25: use a fast lane for small, reversible, directly requested
marketing and documentation edits. Do not turn a wording, spacing, label or minor
navigation adjustment into a ticket/review/integration project.

## Fast lane

Use it for copy, styling, layout adjustments and links to existing approved
internal destinations. The change must not alter executable behavior, dependencies,
authority, security claims, billing, data handling or deployment configuration.

1. Inspect the working tree and current ownership. Use an isolated branch or
   checkout; preserve other workers' changes and claims.
2. Make only the requested edit. Keep an existing visual/design baseline unless
   the user requested a redesign. Record the before/after when it helps review.
3. Run the smallest meaningful checks once: static build/link checks for markup;
   browser checks at affected mobile/desktop sizes for layout/navigation; rendered
   links for docs. Recheck media only when its files, controls or layout changed.
4. Make a small, itemized commit using the user's identity. Push, integrate or
   publish within the actual session authorization. For an already-authorized
   live marketing site, do not ask again for each small follow-up. Preview-only
   requests remain preview-only. Verify the destination after publication.
5. Report what changed, what was checked and the actual live/preview URL.

A new ticket, atomic implementation claim, PR, second model review, integration
batch or hosted-CI approval is **not mandatory** for these direct small requests.
Use an existing issue when useful, but GitHub tracking/API availability must not
block authorized local edits, previews or publication. If a PR is useful for an
audit trail, it need not enter the feature batch/testnet queue. Do not manufacture
an approval or claim an unrun check passed.

Do not run Rust, Move, Yocto, testnet, full coordinator or three-origin app/docs
release builds for an unrelated HTML/CSS/wording edit. Do not repeat a passing
suite against unchanged relevant inputs just to accumulate more evidence.

## Normal engineering lane

Keep existing ticket ownership, source review and relevant automated tests for
wallet/authentication, signing/permissions, contracts, device runtime/installers,
APIs/schemas, billing, storage semantics, security, dependencies and release/CI
configuration. Unknown or mixed changes use this lane. Size alone is not proof of
low impact. Existing actionable findings still require repair.

An automatically assigned implementation/review/repair worker remains bound to its
job and claim; it cannot self-reclassify a blocked feature or security finding as
cosmetic, merge independently or start unrelated work. Mainnet, hardware, protected
credentials and other publication boundaries retain their existing authorization.
The user-authorized local-test integration rule still governs infrastructure fixes.

## Hosted content checks

Server-side workflow path filters keep known presentation-only HTML/CSS and
ordinary Markdown changes out of engine/platform/profile CI. A content workflow
runs the matching static/docs checks and retains the existing dependency, secret
and configuration scanner. Ordinary code/configuration, policy changes, mixed or
unknown paths retain full CI. Manual and scheduled full checks remain available.

`scripts/content_scope.py` selects CI coverage from the actual git diff, not labels
or a PR author's assertion. It includes removed paths and both sides of renames;
missing/invalid or oversized ranges fail closed and require a full CI run. Classification grants no merge
or deployment permission. Guard tests keep workflow filters and policy exceptions
synchronized. Testnet controllers, wallets, spending and live-release protections
are unchanged; cosmetic changes do not create an originating engine-CI run.

## Unchanged runtime activation

The installed operator entrypoint (`scripts/runtime_activation.py`) retains the
activator's provenance, staged-source compilation and runtime compatibility
checks. It skips the repeated runtime suite only when target bytes, actual
installed files and recorded hashes all match. Changed, missing, untrusted or
malformed inputs receive full validation. The full-suite timeout is 15 minutes;
other timeouts and authority checks remain unchanged.
