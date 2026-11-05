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

The player has native caption controls, captions off by default, no autoplay, a phone-specific portrait video,
English captions, an MP4 fallback and the complete transcript. Plain diagrams,
full narration and readable text provide alternatives; colors alone never
explain a state. No third-party scripts or remote assets are introduced.

Each chapter draws only the actual participating components. There is no
repeated corner legend or separate main-stage copy of those components. The
console assembles a release, its wallet signs the request sent to Sui, software
leaves the console's package and enters Walrus storage, the device checks the
Sui record and receives its response, and files travel from Walrus into the
device. Verification and installation operate on that same receiving device;
its result and separately sampled health flow through Sui into the console.
The final scene connects the complete system in sequence. Console/device remain
on the left and Sui/Walrus on the right; relevant actors stay in that relative
orientation as the camera framing changes. Both aspect ratios use that same
orientation, with larger vertical separation on phones. The professional female
narration and optional native captions are preserved.

`site/tests/motion_browser.cjs` compares core-scene pixels at different times,
excluding the header and progress line, and checks named transfer endpoints
against the actual participating actors. It checks relative orientation and
text bounds at four times in every scene and aspect ratio. Pixel changes and
structural checks do not establish animation quality; inspect the scene frames
and the final encoded video too.

For a completed matching frame render, `--reuse-frames` verifies its timeline and
scene/renderer source receipt and exact frame count, then regenerates narration
from the pinned engine before encoding. A stale receipt is rejected.

The hosted static service does not advertise video byte ranges. The exact
integrity-pinned local guide/player script therefore buffers the reviewed
same-origin MP4 after playback begins, verifies its SHA256 and hands it to the
native player as a local Blob. That enables normal seeking without an application
server or another video service. Captions remain native and optional. No download
starts before playback, no credentials are sent, and the script never loads a
remote URL. Its CSP permits same-origin fetch and Blob media only on this page.
If preparation fails, ordinary streaming and the transcript remain available.
The no-range browser fixture exercises this path and rejects tampered bytes.
