# Customer system overview

This is an original illustrative animation, not app-control footage or a claim
of hardware/Testnet acceptance. It precedes the task guide through the home
page's Watch how it works link and the guide's overview link.

Seven scenes explain the fleet, wallet approval, publishing and confidential
file access, device-initiated checks, verified downloads/platform installation,
separate results/health, and the complete system. The console uploads files;
the wallet approves authority. Device checks are outbound. Installation does
not imply current health; offline catch-up depends on deployment eligibility
and file availability. Reboot/rollback remain platform responsibilities.

`storyboard.json` owns the complete reviewed narration and headings. `scene.html`
renders original code-native vector diagrams using the existing three-boat
brand and Inter. No image-generation service or third-party stock footage is
used. `render_frames.cjs` produces actual canvas frames at20fps in landscape
1280×720 and portrait720×1280, refusing all external browser requests.
`render_overview.py` uses the existing hash-verified local Kokoro Heart female
voice, then the pinned network-none ffmpeg image for MP4/WebM and full decoding.
The output records source/media hashes and exact scene-level caption timings.

```sh
PYTHONPATH=/path/to/kokoro-packages CHROME_EXECUTABLE=/path/to/chrome \
python3 site/overview/render_overview.py \
  --runtime /private/new-overview-render --out /private/new-overview-output \
  --model /private/kokoro-v1.0.onnx --voices /private/voices-v1.0.bin
```

Output must be new; models stay outside Git. Use the model/voice hashes and
kokoro-onnx version already pinned by `site/guide/narration.json`. The code/model
license information is retained in `site/guide/RECORDING.md`. Review the complete
spoken text, frames and output before replacing `site/media/overview`. Changing
render source or storyboard invalidates build checks until regeneration.

The player has native controls, no autoplay, a phone-specific portrait video,
English captions, an MP4 fallback and the complete transcript. Plain diagrams,
full narration and readable text provide alternatives; colors alone never
explain a state. No new page scripts or remote assets are introduced.
