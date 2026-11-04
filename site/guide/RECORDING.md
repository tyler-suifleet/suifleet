# Testnet guide recording (SF-175)

The recorder drives the actual console static export with a scripted Wallet
Standard wallet, standing in for an extension wallet. It does not establish
manual Slush, Google, extension, production-provider, physical-device or mainnet
acceptance (SF-031/#32 and SF-087/#159).

Use the pinned development toolchain and locked frontend dependencies. Build the
chain_facts example and debug daemon as described in TESTNET.md. A matching,
activated package-upgrade fixture must already exist in the dedicated wallet's
live-state.json; this tool never publishes packages. It rechecks chain identity,
UpgradeCap ownership, registry and type origins before using the fixture.

Read-only preflight (no lease or spending; also checks wallet readiness):

```sh
python3 site/guide/record_guide.py --check \
  --wallet /home/tyler/.config/suifleet/testnet/agent/client.yaml \
  --runtime-dir /private/guide-check
```

For an actual recording, acquire `network:suifleet-testnet` with the atomic ticket
CLI, coordinate in SF-010/#11, and export its token as
`SUIFLEET_TESTNET_LOCK_TOKEN` without printing it. Assert before mutations, renew
the resource and claim every five minutes, and unlock after the run. The script
asserts the lease before operator mutations and every browser signature.

```sh
python3 site/guide/record_guide.py --fresh-device \
  --wallet /home/tyler/.config/suifleet/testnet/agent/client.yaml \
  --runtime-dir /private/guide-release \
  --max-spend-mist 250000000 --evidence /private/guide-release/evidence.json
# The command above also encodes and checks the site.
# Use --capture-only to retain frames without encoding during diagnosis.
```

Choose the console build by checking out its commit in the recorder worktree;
the runner rebuilds the console with the resolved manifest. `console_commit` is
recorded in evidence and every video's caption. The host must supply Node,
Playwright/Chromium (`CHROME_EXECUTABLE` if needed), and Docker for the pinned
network-none ffmpeg encoder. No DNS or production publication occurs.

The operator allowance is conservative and non-replenishing. It includes the
entire 0.22-SUI browser grant, its deposits and claim sponsorship; rebates and
retained funds do not replenish it. Existing demo gas is charged to the next
run allowance before any top-up. Browser funds are isolated from the operator key.
Only the generated demo account and device public address enter the page. The
signer stays in Node; claim credentials are downloaded and deleted unread.
Transaction digests are journaled before browser signing, then independently
read back with effects and net gas. Unknown outcomes retain their key, digest and reservation and stop replay. Never replay a failed run blindly. Reconcile every
recorded digest, including failed effects, before explicitly recovering its
pending key; runtime directories must be empty for a fresh run.

After resolving every signed digest, `--resume` retains completed clips and the
original manifest, console source, start balance, allowance usage and ceiling.
It never requests a second faucet or automatically replenishes demo funds.
If simulation establishes a need for extra gas, an explicit
`--resume-top-up-mist N` reserves that funding and its transaction gas against
the SAME remaining allowance. Unknown outcomes stop replay; a resume never
raises the ceiling. Each resumed phase keeps its own immutable evidence file.
The current catalog deployment simulated at 51,730,401 MIST net on 2026-10-27;
its 100M gas reservation replaces the prototype's inadequate 20M/40M caps.
Gas reservations are capped below the demo's available coin balance.

The runner retains a private demo identity and reuses its fleet, registered
publisher catalog, group and access policy. `--fresh-device` records a real new
registration with a private run-owned device key; omit it to reuse the existing
demo device when a registration clip is not being regenerated. A new recording directory
names a new demo component version. Enrollment claims and rollouts are run-owned
examples. A retained key without matching, reconciled evidence stops the run;
never recover a key or repeat an uncertain transaction blindly.

Each WebM and MP4 must be at most 15,000,000 bytes; nine steps therefore have a
270-MB maximum combined video budget, plus posters and text. MP4 is H.264 with
faststart, WebM is VP9. The initial encoder produces silent clips with burned-in
captions; the narration pass below adds the spoken track. Encoding shortens
long confirmation waits, without claiming instant network confirmation. Written
instructions appear in each step's transcript disclosure. The offline site checker
verifies media hashes and links; `--require-videos` additionally requires every
step, poster and fallback. Never publish placeholder or mock videos as acceptance.

Before committing media, review every video from start to finish for accuracy,
caption readability, keys, credentials, tokens and non-demo addresses. Keep raw
frames, browser state, keys and failed captures private outside the checkout.
Record review evidence and exact console commit with the transaction/cost receipt.
Do not mark SF-175 Review until the genuine nine-step run and review pass.

The recording-only overlay in `recording_overlay.cjs` paints a high-contrast
pointer into compositor frames. It follows actual pointer events, including
composed events from wallet shadow roots; clicks create a short amber pulse.
The helpers move the pointer visibly before clicking. The overlay never accepts
pointer events or focus and is not part of the shipped console. The offline
recording browser check verifies captured pixels and real screencast frames,
pointer persistence, click-through behavior and shadow-root wallet interaction.
Existing videos must be re-recorded to include it; do not infer historical mouse
trajectories or claim a recorder fix retroactively changes published clips.

## Female voice-over

`narration.json` contains the reviewed spoken script and cue positions in the
original cursor clips. `narrate_guide.py` uses the synthetic American English
female Kokoro Heart voice (`af_heart`), locally. It sends no text or visitor data
to a speech service and does not contact the chain. It lengthens individual action
segments when necessary, preserving natural-speed actions and holding their last
frame while explanations finish, with a short pause between cues. Every section
explains what it is for, when to use it, the steps shown and what to check next.
Video timing is illustrative, not a network latency measurement.

Install optional `kokoro-onnx==0.4.9` in an isolated Python environment. Download
`kokoro-v1.0.onnx` and `voices-v1.0.bin` from the
[upstream model release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0).
The script verifies the pinned SHA-256 digests in `narration.json` before loading
either file. Keep these model files outside the checkout. The
[upstream project](https://github.com/thewh1teagle/kokoro-onnx) identifies its code
as MIT licensed and the Kokoro model as Apache 2.0.

```sh
python3 site/guide/narrate_guide.py \
  --source-media /private/original-cursor-media \
  --output /private/narrated-media \
  --model /private/models/kokoro-v1.0.onnx \
  --voices /private/models/voices-v1.0.bin
```

The source directory must contain the original verified `videos.json` and all
media. The output must be new and separate; rerunning on narrated clips is
rejected by source digests. Review cue times again after any new recording.
The pass verifies source hashes, audio presence/codecs, durations, the 15 MB cap,
and full decoding of both outputs. WebM uses Opus and MP4 uses AAC. Public
`videos.json` records synthetic voice/model provenance and the retimed script.
Preserve the actual console commit, chain receipts, cursor and posters.

After reviewing all nine outputs, replace `site/media/guide/` with the staged
media, then run the site build/check with `--require-videos`. Native playback
controls manage volume; no autoplay or third-party player is needed. The written
guide and burned-in captions remain available without sound. No model or
synthesis dependency is included in the static website.
