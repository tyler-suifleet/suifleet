# Mainnet guide capture (SF-289)

Preparation is ready for offline rehearsal. Mainnet capture, physical acceptance
and publication are pending. The published guide still uses its historical
[testnet recordings](RECORDING.md). Final acceptance lives in [SF-031][device]
and [SF-087][release]. Record capture evidence in [SF-289][capture].

## Before recording

1. Obtain explicit authorization for the mainnet actions, approved demo wallet
   and fleet, all spending currencies and ceilings, and separately for any
   physical work. Record the approval reference. Use an isolated browser
   profile and a dedicated, limited-balance demo wallet. Keep release authority,
   customer accounts and unrelated devices outside the demo.
2. Confirm the approved HTTPS release at `https://app.suifleet.io`.
   Compare its console revision and export checksum inventory to the approved
   deployment receipt. Save the exact served `deployment-manifest.json`.
   Independently read chain identity `35834a8a`, the package and version, shared
   registry and type origins against the [deployment manifest](../../docs/DEPLOYMENT_MANIFEST.md).
   Verify the fleet and wallet authority, daemon release and manifest before use.
   Keep the independent release verification reference.
3. Write down each proposed action, maximum charge and projected remainder.
   Include funding, purchases, storage, fees, failed executions and other
   charges. Reserve before each signature. Refunds and rebates do not increase
   the allowance. Stop before exceeding any approved ceiling. If effects are
   unknown, reconcile by digest before continuing or retrying. Keep separate
   ledgers for non-SUI charges and retain them with the approval record.
4. Prepare a small, reviewed, architecture-appropriate component that writes
   a harmless version marker inside the demo component's directory. Hash the
   actual artifact, retain the recipe digest and build/source details, and use
   the matching authorized demo access policy. Avoid services, firmware,
   privileged commands and reboot.

## Capture the deployed app

Use OBS with a browser-window or isolated-display capture source and cursor
capture enabled. Set the output canvas to 1280×800, 30 fps, and record MKV in a
mode-0700 directory outside the checkout. Rehearse the same setup offline with
a clearly labelled rehearsal clip. Check the cursor is legible in the recorded
pixels and that wallet UI intended for publication falls inside the frame.
Turn off notifications and system audio. Stop or hide capture before entering
credentials, revealing a payment identifier or opening a claim download.
Keep clean footage, raw footage, review records and credentials separate.
Use the real wallet and deployed app without injected wallet code, request
fixtures, overridden network responses or seeded success states.

Use [the guide stages](steps.json) as the shot list. Confirm the actual deployed
UI labels before writing the narration. Show sign-in and fleet selection,
capacity checkout, budget funding and first-device registration. Film bulk
enrollment as optional and explain access policy before publication. Show the
published version, deployment and its actual device result. Film staged rollout
as optional and report only the observed state. Show how to review fleet
settings and close with the next action.
Use a woman narrator to explain both the purpose of each action and what the
viewer should check, in plain language. If shortening a wait, label the edit.
Keep captions out of the picture and time the optional English track to the
final speech.

Retain every submitted digest and the independently read effects. Verify the
deployment target and authoritative Applied receipt for each demonstrated device
against its daemon version marker and logs. A digest or a process exit alone
is insufficient. Record device identities separately from physical hosts.
For multiple identities on one or a few authorized demo hosts, use isolated
key, config, journal and component paths per identity. Record the host mapping,
daemon build and whether execution was physical or synthetic. A logical identity
running in a container remains synthetic even if the host is a real device.
Describe the exact host and identity counts, and avoid generalizing to untested
hardware or recovery paths.

## Stage and review

Export each edited, redacted master with final narration. Use the pinned
network-none FFmpeg environment described in [RECORDING.md](RECORDING.md).
Export VP9/Opus WebM and H.264/AAC MP4 (yuv420p, faststart), with a JPEG
poster at matching dimensions. Keep each video below 15 MB, and fully decode
both formats. Name the assets `<step>.webm`, `<step>.mp4`, `<step>.jpg` and
`<step>.en.vtt`. Generate WebVTT with `captions.webvtt(cues, duration_seconds)`
in [captions.py](captions.py) using final speech start and end times in seconds.

Keep `capture.json`, the deployed manifest and final media in the same private
directory. Use this record shape for the offline check. Strings in angle
brackets and the listed evidence values are placeholders.

```json
{
  "schema": 1,
  "source": "deployed-app",
  "origin": "https://app.suifleet.io",
  "observed_chain": "35834a8a",
  "authorization_ref": "<approved actions, fleet, wallet, ceilings>",
  "release_verification_ref": "<verified deployed build, daemon, manifests and recipe>",
  "media_review_ref": "<reviewer, date, clip hashes, phone/browser models and results>",
  "manifest_sha256": "<served deployment-manifest.json SHA-256>",
  "console_export_sha256": "<approved release export SHA-256>",
  "console_commit": "<40 hex commit>",
  "artifact_sha256": "<deployed demo artifact SHA-256>",
  "demo_fleet": "<canonical fleet ID>",
  "demo_wallet": "<canonical wallet address>",
  "approved_mist": 0,
  "other_mist": 0,
  "transactions": [
    {"digest": "<real digest>", "effects": "success", "charged_mist": 0}
  ],
  "device_results": [
    {
      "device": "<canonical device ID>",
      "deployment": "<canonical deployment ID>",
      "host": "<non-identifying demo host label>",
      "execution": "physical",
      "status": "Applied",
      "receipt_transaction": "<real digest>",
      "daemon_log_sha256": "<private daemon evidence SHA-256>"
    }
  ],
  "clips": [
    {
      "step": "deploy-group",
      "transactions": ["<real digest>"],
      "duration_seconds": 4,
      "cues": [{"speech_start": 0.5, "speech_end": 3, "text": "<actual speech>"}],
      "sha256": {"mp4": "<hash>", "webm": "<hash>", "jpg": "<hash>", "en.vtt": "<hash>"},
      "review": {
        "cursor_visible": false,
        "redaction": false,
        "woman_voice": false,
        "ui_labels": false,
        "captions_toggle": false,
        "mobile_playback": false
      }
    }
  ]
}
```

Record positive charges without refund offsets in `charged_mist`. Use
`other_mist` for SUI charges outside the submitted transaction list, with the
breakdown in the retained ledger. Include failed effects in the run ledger, but
only successful digests in clips that show completed actions. Attach each shown
digest to its stage and the demonstrated Applied receipts to `deploy-group`.
Provide one clip record per guide stage. Keep the raw chain and daemon evidence
for independent verification.

Review the final assets from start to finish for factual UI labels, cursor,
speech, sensitive pixels/audio and customer information. On mobile, use a private
preview of the staged guide player, test play, pause, seek, readable text and
captions off/on, and check the MP4 fallback. Record the browser, device,
viewport and actual result. Enable the individual review checks only after
seeing the matching final bytes.

```sh
python3 site/guide/check_mainnet_capture.py /private/mainnet-guide
```

This offline command checks internal consistency and binds the reviewed media to
the record. The independent reviewer still confirms the deployed app, chain
effects, spend and device results using the retained evidence. Keep the private
bundle outside source control. After acceptance, prepare a separate media change
with the new provenance and sanitized receipts, update the guide manifest and
its historical narration/source checks to match the new recordings, and run
`site/check.py --require-videos` against the built site and the guide browser
suite. Publish only the reviewed media and public provenance after release
authorization.

[device]: https://github.com/tyler-suifleet/suifleet/issues/32
[release]: https://github.com/tyler-suifleet/suifleet/issues/159
[capture]: https://github.com/tyler-suifleet/suifleet/issues/811
