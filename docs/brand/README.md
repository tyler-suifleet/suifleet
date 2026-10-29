# SuiFleet visual identity

The user selected concept 09 on 2026-10-27: three sky-blue sailboats moving together on one wave. The site uses this fleet direction with a blue wordmark in locally served Inter. The whole site follows the same direction: white and pale-sky surfaces, ocean-blue headings, blue actions, softer rounded cards and a locally served wave illustration. The shared stylesheet applies this identity to every public page. The initial website application is tracked in SF-180. Product copy describes the testnet preview; the example rollout on the homepage is explicitly illustrative.

## Assets

- `site/src/assets/suifleet-logo-concept-09.png`: selected original horizontal logo, copied unchanged from built-in image generation. Retained as the selected reference; this original has a solid white background.
- `site/src/assets/suifleet-mark.png`: transparent symbol-only derivative produced with built-in image editing from concept 09; no raster postprocessing. Available through the footer’s “Download transparent logo” link as `SuiFleet-mark.png` (RGBA PNG with alpha transparency).
- `site/src/assets/logo.svg`: manually authored vector interpretation of the selected three-sail/shared-wave motif, used for crisp navigation and small-size rendering.
- `site/src/assets/favicon.svg`: the same vector motif for browser tabs.
- `site/src/assets/fleet-wave.svg`: manually authored decorative waves echoing the shared wave in concept 09; no runtime scripts or external resources.
- The wordmark is accessible live text. The vector interpretation is not an exact pixel trace of the generated reference.

Symbol color: sky blue `#4da2ff`. Live wordmark and actions: deeper blue `#1768b2` for readable contrast on white. Heading ink: `#15304c`; body copy: `#536b82`; surface: `#f4f9fe`. The security section uses deep ocean blue `#103b64` with pale-blue text. Amber remains reserved for update/warning meaning. Avoid stretching, shadows or extra outlines.

## Font provenance

Inter Variable is self-hosted, with no request to a font provider. Source: `https://github.com/rsms/inter`, revision `353b61b9f4430d5f420d56605a6e7993e0941470`, file `docs/font-files/InterVariable.woff2`. SHA256: `693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3`. The bundled SIL Open Font License is `site/src/assets/inter-LICENSE.txt`. The only CSP addition is `font-src 'self'`; the script-free policy remains intact.

## Logo generation provenance

Concept 09 was generated using the earlier boat and outlined-drop concepts as references: refine three elegant abstract sails moving together with one shared curved water line, single sky blue on white, horizontal SuiFleet wordmark, simple flat geometry. The user selected it with “Let’s use 9 for now.”

The transparent derivative requested extraction of only the three-sailboat fleet symbol, preserving the increasing sails, hull slivers and shared wave, removing the text and white background. Original generated files remain preserved outside the repository as well.
