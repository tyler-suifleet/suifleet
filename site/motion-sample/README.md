# Twenty-second motion direction sample

An independent Remotion 4.0.541 / React composition in the user-selected
woman-at-the-computer editorial illustration direction. One consistent factory
scene replaces the separate product renders. `art/editorial-reference.png`
records the chosen direction; `art/editorial-scene.png` is a clean plate generated
with built-in `image_gen`. The exact edit brief is in `art/editorial-prompts.json`.
The originals are preserved; branding, text, parcels, signatures, routes and
verification marks are drawn separately in SVG. No image-generation API key or
paid rendering service is required.

The scene itself is illustrated artwork, not app footage or a hardware claim.
The animation uses progressive paths, moving software parcels, approval seals,
signing, device queries/responses, on-device verification and camera movement.
Approval metadata goes to Sui; software uploads directly to Walrus and downloads
to devices. Software is never depicted passing through Sui to reach storage.
Devices initiate their checks. Every verified state is added after receipt;
none is baked into the clean plate. The scene ends at “Ready to install,” without
claiming that the platform installer ran. The spoken verb is “saves,” preserving
the pronunciation correction. The full overview remains independent.

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
