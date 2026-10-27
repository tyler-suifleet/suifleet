# SuiFleet visual identity

The site uses a navy and sea-green fleet-in-formation mark, the SuiFleet wordmark in locally served Inter, and a restrained white / pale-green / navy palette. The initial website application is tracked in SF-180. Product copy describes the testnet preview; the example rollout on the homepage is explicitly illustrative.

## Assets

- `site/src/assets/suifleet-mark.png`: original generated concept,1254 ×1254 RGBA; transparency preserved without resizing or editing. Built-in image generation was used.
- `site/src/assets/logo.svg`: manually authored compact vector companion for crisp website navigation and small-size use.
- `site/src/assets/favicon.svg`: the same compact mark for browser tabs.
- The wordmark is live text, so it stays sharp and accessible. Use the navy mark on light backgrounds; the source PNG is available from the footer download link.

Colors: ink `#10252e`, green `#43b994`, light surface `#f4f6f2`. Avoid stretching, extra outlines, drop shadows or placing the dark mark on an equally dark background.

## Font provenance

Inter Variable is self-hosted, with no request to a font provider. Source: `https://github.com/rsms/inter`, revision `353b61b9f4430d5f420d56605a6e7993e0941470`, file `docs/font-files/InterVariable.woff2`. SHA256: `693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3`. The bundled SIL Open Font License is `site/src/assets/inter-LICENSE.txt`. The only CSP addition is `font-src 'self'`; the script-free policy remains intact.

## Logo generation prompt

Create a single finished logo SYMBOL for SuiFleet, an enterprise edge-device fleet management and secure software deployment platform. Brand identity should feel like precision industrial software and excellent contemporary Swiss graphic design, confident, exceptionally simple, distinctive and professional. Design a compact abstract F made from two or three coordinated forward-moving geometric bands, suggesting a fleet moving in formation and a controlled route. Strong negative space, optically balanced, readable at 24 pixels, clean flat vector-like edges. Primary ink very dark navy #10252e, with one small restrained sea-green #43b994 accent; no gradients, no shadows, no 3D, no outline box or circle container, no generic hexagon/cloud/blockchain chainlink/rocket/droplet motifs. SYMBOL ONLY, no letters or text outside the symbol, no wordmark, no mockup, no sheet of variants. Genuinely transparent background. Center the single mark and fill about 85 percent of the square canvas so it can be used directly as a website brand icon. Produce a polished production brand asset, not a sketch.
