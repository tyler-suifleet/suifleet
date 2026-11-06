# Twenty-second motion direction sample

An independent Remotion 4.0.541 / React / SVG composition, with original vector
artwork, spring-driven entrances, a continuous camera move and self-hosted Inter.
It explores a visual direction; it does not replace the full system overview.
The console, approval record, software storage and device belong to one spatial
scene. Completed steps fade as the camera follows the active step. Verification
ends at “Ready to install,” without claiming the platform installer ran.

The 20-second film has 600 frames at 30 fps in landscape 1600×900 and portrait
900×1600. `script.json` owns the narration. `prepare.py` verifies the existing
local Kokoro model/voice hashes, uses the female `af_heart` voice, rejects
truncated/overlapping speech and prepares the ignored local assets. It requires
the Kokoro Python packages and model cache described in `site/guide/RECORDING.md`.
No speech service, paid rendering account or remote media assets are used.

With the pinned Node 22.22.2 toolchain on PATH:

```sh
npm ci --prefix site/motion-sample --ignore-scripts
PYTHONPATH=/path/to/kokoro-packages OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  python3 site/motion-sample/prepare.py
node site/motion-sample/render.mjs /private/new-render-directory --stills-only
node site/motion-sample/render.mjs /private/new-final-render-directory
```

Review stills are actual Remotion frames. Full rendering uses the installed
Chrome browser and writes both MP4s, five stills per variant, source/asset hashes,
narration timings and a render receipt. Keep output outside Git. Before copying
media into `site/media/motion-sample`, verify both video/audio streams and decode
the complete files. The committed manifest adds caption/poster hashes; the
stdlib site builder verifies sources, assets, media and captions before copying.
Normal site builds use those checked artifacts and need no Remotion install.

The review URL is `/film-preview/`, with `noindex, nofollow` and no navigation or
sitemap entry. Playback is native H.264/AAC, no autoplay, optional English captions
and direct downloads. `test_browser.cjs` accepts either a built-site directory or
the owned HTTPS origin and checks both responsive players, duration, captions,
seeking and encoded visual development. The existing full overview stays intact.

Remotion's [license terms](https://www.remotion.dev/docs/license/pricing) apply;
the npm versions and transitive dependencies are pinned in `package-lock.json`.
