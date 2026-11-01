# SF-175 guide recordings

The site includes nine real testnet console clips, each as WebM and H.264 MP4,
with a JPEG poster, burned captions, written transcript/instructions and its
console commit. The [manifest](../../site/media/guide/videos.json) binds every
asset to a SHA-256 digest. Total media is 9,293,464 bytes; the largest video is
1,164,514 bytes, below the 15,000,000-byte per-format limit. Clips run about
12–69 seconds (the original device registration is about 14 seconds).

The [transaction receipt](sf175-testnet.json) records chain `4c78adac`, package,
registry, type origins, pinned toolchain, every operator/browser transaction,
independent successful effects, gas, funding and allowance usage. The main
recording resumed under its unchanged 250,000,000-MIST ceiling and used
224,976,284 MIST of conservative allowance. Its 17 transactions cost
202,842,035 MIST in network fees. The delivered registration clip uses its
original successful transaction from rehearsal g (27,616,237 MIST gas), with
that clip's exact console commit displayed. All rehearsal fees, including
incomplete attempts, total 293,065,654 MIST. Funding and deposits are distinct
from fees and are recorded separately; no faucet or rebate replenished an
allowance.

The console commits are `755a7954bffa8594af9515ac3a25ec8be6330ebc` for the
initial registration and `79675b48e6bd3d17b22bf919e0953f389535edf0` for the
other clips. `git diff --quiet` over `dapp` between those commits passed:
the console source trees are identical. The initial registration footage was
retained rather than spending gas to register that same device again.
`--fresh-device` records a new run-owned device for a fresh release recording;
other demo fleet objects are reused. Its signing key stays private.

## Verification

- Locked frontend dependencies installed with pnpm 11.0.8 in the dev image.
- Actual console production builds succeeded with the resolved testnet manifest;
  Node 22.22.2 was used for the completed recording. Frontend
  `node node_modules/typescript/bin/tsc --noEmit` passed.
- `make test-site test-guide-recording` in the pinned image with `--network none`:
  23 Python checks and browser regressions passed. They cover state preservation,
  unchanged resume allowance and in-flight reservations, token-redacted lease
  refusals, unsafe frame input, controlled form input and shadow-root reconnect.
- `python3 site/build.py` and `python3 site/check.py --require-videos` passed.
- `node site/tests/render.cjs` in the pinned image passed all 18 page/viewport
  checks, including media posters, mobile layout and no third-party requests.
- All 18 encoded files decoded with the expected codec, 1280×800 dimensions and
  no audio. Sizes and durations are retained in the receipt.
- Decoded chronological filmstrips of every video and final posters were visually
  inspected for captions, action/result accuracy, keys, tokens and non-demo
  accounts. No such secret/account leak was observed. This is agent visual
  review, not a human extension-wallet/provider session. Claim credentials were
  downloaded and deleted unread; only synthetic names and demo public addresses
  appear in the footage.
- The real shared-wallet lease was released before encoding and review.

## Recording limits

The recording was developed through actual rehearsals and resumptions, rather
than represented as an uninterrupted final-head run. Recorder selector, gas and
journal repairs preserved/reconciled the real outcomes. An unused rollout and an
empty duplicate demo group remain retained and charged; the historical rollout
creation was independently recovered from its record's `prevTx`. No original
application objects were changed. The current recorder refuses unrecognized prior
rollout records and keeps separate resumed phase journals.

The host/compositor clock went backwards during capture. Original timing journals
remain private; delivered frame order and actual pixels were preserved while
presentation intervals were bounded to 0.04–1.6 seconds. The rollout clip preserves
its creation footage and explicitly shows returning to the saved rollout for the
advance. Current captures use a monotonic arrival clock. Long waits are shortened;
the videos do not represent real network-confirmation latency.

The public example was published unencrypted; the policy clip establishes an
actual on-chain group grant, not provider decryption acceptance. Enrollment shows
credential preparation and claim registration, not device claim redemption.
The rollout shows creation and admission/advance, not device execution or healthy
boot. Manual real-wallet/provider, physical and mainnet acceptance remain unrun
in [SF-031](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087](https://github.com/tyler-suifleet/suifleet/issues/159). Hosting publication,
production credentials and physical programming were not performed.

Fresh exact-head CI and independent review remain coordinator merge gates.
SF-172 remains this ticket's merge prerequisite. No hosted CI was polled or
claimed passing by the implementation worker.
