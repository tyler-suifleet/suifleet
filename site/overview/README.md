# Customer system overview

A 96-second editorial animation based on the user-selected woman-at-the-computer
scene. Eight chapters explain the fleet, secure approval, publishing, device
checks, verification, platform installation, results and staged rollout. The
woman, console and factory devices share one continuous visual setting; editable
routes, parcels, approval, installation progress and result cards animate over
that illustration. The source plate and exact built-in image edit prompt are in
`site/motion-sample/art/`. Canonical three-boat branding remains code-native.

`storyboard.json` owns the conversational narration and chapter headings.
`site/motion-sample/src/overview.tsx` is the full Remotion composition, separate
from the approved 20-second sample. The pinned toolchain and local female
`af_heart` voice are shared with that sample. Sui carries approval information;
software goes directly to Walrus and then to the devices. Device checks are
outbound. Installer/reboot/recovery remain platform responsibilities. Result
cards distinguish failed installations from separately sampled health. All
scenes are illustrative, not evidence of real device or wallet acceptance.

With pinned Node 22.22.2 on PATH and the existing narration packages available:

```sh
PYTHONPATH=/path/to/kokoro-packages OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
python3 site/overview/render_overview.py \
  --runtime /private/new-overview-render --out /private/new-overview-output \
  --model /private/kokoro-v1.0.onnx --voices /private/voices-v1.0.bin
```

`--prepare-only` checks narration timing and prepares inputs for review stills.
`node site/motion-sample/overview-render.mjs /private/review --stills-only`
produces an actual frame from every chapter in both aspect ratios. A normal run
renders 1280×720 and 720×1280 at 30fps, then encodes WebM with the pinned
network-none ffmpeg image. Both complete formats are decoded before acceptance.
`--reuse-render` requires exact source, asset and audio hashes from the retained
input receipt. Output must be new. Keys and chain calls are never used.

The manifest binds renderer, storyboard, toolchain, artwork, font, brand,
narration, captions and media. Website builds reject stale inputs. Native
captions are optional and off by default; the complete transcript is available
below the player. No autoplay or third-party scripts/media. The previous canvas
renderer is retired; `site/tests/overview_browser.cjs` validates the actual
encoded film, including movement in every chapter, caption controls, seeking,
MP4 fallback and integrity failure. It accepts `OVERVIEW_ORIGIN` for real HTTPS
checks as well as local builds.
Remotion license information is retained in `site/motion-sample/README.md`.
