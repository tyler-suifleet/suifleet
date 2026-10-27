# Guide media follow-up (SF-175)

SF-172 ships the written guide and an offline console screenshot. No testnet
video has been recorded or approved here. SF-175 owns scripted per-step testnet
screencasts and must satisfy its spending, credential isolation and review gates.
The earlier unvalidated recorder prototype and its unfinished fixes remain on
`work/suifleet/sf-172-ac70d597` at d5306c76 and in the preserved operator patch;
they are not a tested release tool. Review before reuse; never replay unknown
transactions or sweep an unverified key.

The optional `site/media/guide/videos.json` interface is retained: schema_version,
network=testnet, chain=4c78adac, console_commit, recorded, and videos (step, file,
file_sha256, optional poster/poster_sha256, width, height, caption). Add all seven
steps and enable `--require-videos` for the recording acceptance check.

Before publishing media: verify current console labels against steps.json; capture
only disposable testnet identities; inspect every frame and caption for keys,
claim credentials, tokens and personal data; record console revision, chain,
transaction effects and costs; then run the build/check and review every video.
Manual real-wallet acceptance remains separately tracked, not established by a
scripted wallet recording.

## Reproduce the offline screenshot

Build the console at the recorded revision with its verified testnet manifest,
using the pinned toolchain and locked dependencies described in OPERATIONS.md.
Run `node site/guide/capture_console.cjs /absolute/path/to/dapp/out`. Requires the
console's Playwright dependency; `SUIFLEET_PLAYWRIGHT_MODULE` can name an existing
installation and `CHROME_EXECUTABLE` a local Chromium. The script serves on an
ephemeral loopback port, blocks external requests and captures the signed-out
Get started page. It never connects a wallet, handles keys or sends transactions.
The committed screenshot uses the preserved d5306c76 console export (console
source unchanged at recovery). Font/network fallback can differ across hosts.
