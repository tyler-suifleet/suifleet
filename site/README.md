# suifleet.io static site (SF-172)

Static product pages, a written console guide, an offline console screenshot,
proposed pricing, documentation entry, security/custody and contact/privacy.
Standard-library Python 3.10+; no install or network needed to build:

```sh
make test-site site-check
python3 site/build.py --out /tmp/suifleet-site
python3 site/check.py /tmp/suifleet-site
```

The build uses src/layout.html, src/site.json and src/pages/*.html. Guide text
comes from guide/steps.json. Output is sorted and timestamp-free with SHA256SUMS.
For an optional browser check, run `node site/tests/render.cjs` after building;
it uses the console’s installed Playwright and `CHROME_EXECUTABLE` if set, checks
nine pages at 375/1280px, and saves screenshots for visual review.

CI builds twice, compares bytes, and checks internal links/fragments, repository
paths, allowed HTTPS link hosts, page metadata and checksums. External URLs are
not fetched: release review must follow those links. Scripts, inline handlers,
embedded frames, forms and third-party HTML resources are rejected. There is no
analytics code or consent-dependent tracking. The host may keep access logs;
confirm its actual privacy terms before publishing the notice.

## Scope and shared interfaces

The user split issue #338 on 2026-10-27:

- SF-175 owns testnet screencasts; no videos are claimed recorded here. See
  [guide/RECORDING.md](guide/RECORDING.md) for the media contract and screenshot reproduction.
- SF-176 owns hosting, DNS, TLS and authorized production publication.
- SF-177 owns the rendered docs site under site/docs. Until then, the Docs page
  links to repository documentation, including device installers.
- SF-178 owns pricing generated from the canonical model. The present page is
  explicitly proposed and mirrors docs/BUSINESS_PLAN.md, not a binding tariff.

Preserve build.py's standard-library renderer, src/layout.html token contract,
src/site.json and src/pages/pricing.html for the parallel docs/pricing workers.
Optional media manifests remain supported; `python3 site/check.py site/dist
--require-videos` is the additional SF-175 acceptance gate, not the SF-172 build.

## Hosting and DNS plan (proposal, not deployed)

Use conventional static hosting for the three origins, with no application
server. Select and verify the provider in SF-176; it must support custom domains,
managed TLS, redirects and the response headers in src/_headers. A host that
ignores that file needs equivalent explicit header configuration. The existing
Walrus Site console publication remains a separate verifiable distribution path;
this plan neither publishes it nor configures a custom-domain portal.

| Name | Artifact / behavior |
| --- | --- |
| suifleet.io | site/dist, apex alias/flattened CNAME as required by selected host |
| www.suifleet.io | CNAME plus permanent redirect to the apex |
| app.suifleet.io | dapp/out from a verified network deployment manifest |
| docs.suifleet.io | SF-177 static documentation artifact |

Verify registrar ownership, DNSSEC support, host targets, CAA issuers and issued
TLS certificates before configuring records. Do not guess provider DNS values.
Google/Enoki redirect configuration must match the console origin (see
WALLET_ONBOARDING.md). The planned hello@suifleet.io mailbox needs forwarding,
SPF/DMARC and a successful delivery test before being advertised live. Domain,
mailbox and provider ownership have not been established by this work.

The source's Launch app URL is the planned app origin. None of these links imply
that DNS, a mainnet service, paid subscriptions or production providers are live.

## Release and rollback

Live publication needs explicit user authorization recorded in the release issue;
SF-176 owns that release. Builds and PRs do not authorize publication.

1. Build the reviewed main revision, run the tests and checker, and retain the
   exact artifact and SHA256SUMS. Build the console separately with its verified
   deployment manifest; never include private keys or enrollment files.
2. Review all pages at desktop/mobile widths, follow external links, verify
   contact delivery and the preview/network/pricing/privacy claims. After
   SF-175, watch all recordings and verify their source revision and redaction.
3. Record authorization, revision, checksum digest, provider and DNS/TLS plan.
   Upload the reviewed artifact without a provider-side rebuild.
4. Configure the authorized domains and redirects, then run
   `python3 site/verify_served.py site/dist https://suifleet.io` to verify served
   bytes and required security headers. Record deployment ID and results.
5. Roll back by restoring the retained previous artifact/deployment and repeating
   served-byte/header checks. Record the rollback; do not edit hosted files.

No production publication, network transaction, video recording, real-wallet or
physical-device acceptance has been performed for this static-site work.

SF-176 selects Cloudflare Pages Direct Upload; see
[the hosting runbook](../docs/STATIC_HOSTING.md) for credentials/environment setup,
DNS/TLS change plan, release digests, preview/rollback gates and current blockers.
This provider selection does not establish any live hostname or preview acceptance.

The SF-176 workflow now builds marketing, the committed SF-177 docs builder and
an exact console source revision bound to the testnet deployment manifest. See
`site/prepare_hosted.py`, `site/collection.py` and the runbook for the artifact
contract. Offline fixture restoration is not real preview/rollback acceptance;
provider setup, real preview and recorded-wallet release acceptance remain unrun.
