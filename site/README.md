# suifleet.io landing site (SF-172)

The public entry point for SuiFleet: a static marketing site with the product
explanation, a **Launch app** link, the console guide with screencasts, pricing,
documentation entry, security and custody notes, and contact/privacy pages. It
follows the usual layout for Sui applications:

| Name | Serves | Content |
|---|---|---|
| `suifleet.io` (and `www.` redirecting to it) | this site | `site/` built by `site/build.py` |
| `app.suifleet.io` | the console | the static dApp export (`dapp/out`), built for one network's verified deployment manifest |
| `docs.suifleet.io` | documentation | the repository's `docs/` (see [Documentation domain](#documentation-domain)) |

There is no application server behind any of them.

## Build and check

Standard-library Python 3.10+, no package install, no network:

```bash
make test-site        # unit tests: build, reproducibility, checker negatives
make site-check       # builds site/dist, then checks it with --require-videos
python3 site/build.py --out /tmp/site && python3 site/check.py /tmp/site
```

The build renders `site/src/pages/*.html` into `site/src/layout.html`, expands
the guide from [guide/steps.json](guide/steps.json) and the recorded
[video manifest](media/guide/videos.json), copies assets and writes
`SHA256SUMS`. Output is byte-identical across runs and machines: no timestamps,
sorted file order, LF line endings. CI builds it twice and diffs the results.

`site/check.py` fails on:

- a broken internal link or `#fragment`, or a link without the trailing slash
  that static hosts treat inconsistently;
- an external link that is not `https`, not on the `external_hosts` allowlist in
  [src/site.json](src/site.json), or a GitHub link to a repository path that does
  not exist on this branch (the build also refuses `{{repo:PATH}}` for a missing path);
- any `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, inline event
  handler, inline style or externally hosted image/media/style. The site runs no
  code and loads nothing from third parties, so it has no trackers and needs no
  consent banner. Adding analytics later needs a consent mechanism, a privacy
  notice change and a checker change, all reviewed together;
- a page without `lang`, title, description, a single `<h1>` or the exact
  Content-Security-Policy meta tag;
- `SHA256SUMS` or a guide video/poster that does not match its recorded digest, a
  video manifest not recorded on Testnet (chain `4c78adac`), or (with
  `--require-videos`) a guide step without a screencast.

External links are not fetched in CI. Before a release, open every page and
follow its external links by hand (see the release checklist below).

## Content sources

- **Pricing** mirrors the proposed price list in
  [docs/BUSINESS_PLAN.md](../docs/BUSINESS_PLAN.md#proposed-price-list), labelled as
  proposed. Change both together; the user approves a published tariff.
- **Guide steps** describe the current console. Labels in `guide/steps.json`
  must match the console build that the screencasts were recorded from; the
  recorder drives the same labels, so a renamed control makes recording fail
  rather than leaving a stale guide.
- **Security and custody** summarise
  [ACCESS_AND_ENCRYPTION.md](../docs/ACCESS_AND_ENCRYPTION.md),
  [KEY_ISOLATION.md](../docs/KEY_ISOLATION.md) and the status in AGENTS.md.
  Do not claim launches, audits or hardware protection that have not happened.

## Guide screencasts

Recorded on Sui Testnet by [guide/record_guide.py](guide/record_guide.py)
from the real console static export, following
[guide/RECORDING.md](guide/RECORDING.md). The recorder signs through a scripted
Wallet Standard wallet whose throwaway key stays in the Node process: the page
never receives it, and nothing secret is on screen. The resulting WebM files,
posters and `videos.json` (step, digests, console commit, chain, recording date,
transaction digests) are committed under `site/media/guide/`.

## Hosting and DNS plan

**Decision (proposed, needs the user's approval before any domain is configured):**
serve `suifleet.io` and `docs.suifleet.io` from conventional static hosting, and
serve `app.suifleet.io` from the same host using the reproducible console export,
with the Walrus Site as the independently verifiable copy of the console.

Why conventional hosting for the apex rather than Walrus Sites:

- A Walrus Site is served through a portal. The public `wal.app` portal serves
  only Mainnet sites, under SuiNS names (`NAME.wal.app`). Serving a Walrus Site on
  a custom apex domain means operating a portal, which is an application server
  this product avoids, or depending on a third party's.
- The marketing site has no wallet code to protect, so the on-chain integrity of
  a Walrus Site adds little, while every update costs Mainnet SUI and WAL and the
  storage must be renewed before it expires (see SF-159's expired-binding
  recovery in [SERVERLESS_FLEET_DATA.md](../docs/SERVERLESS_FLEET_DATA.md)).
- The console is different: it signs transactions. Its Walrus Site publication
  path already exists (SF-082) and stays the verifiable copy. At Mainnet launch,
  publish it under a SuiNS name (for example `suifleet.wal.app`) and list its
  Site object ID and build receipt on the security page, so anyone can compare
  the bytes served at `app.suifleet.io` with the on-chain resources.

Recommended host: Cloudflare Pages (or an equivalent that honours `_headers`,
so the CSP `frame-ancestors`, HSTS and `nosniff` headers in
[src/_headers](src/_headers) take effect; GitHub Pages ignores that file and
would leave only the meta-tag CSP). Upload the exact CI-built, checksum-verified
directory with the host's direct-upload CLI. Do not let the host rebuild from
Git: what is served must be the reviewed, reproducible artifact.

DNS records (at the registrar or host DNS, with DNSSEC enabled and registrar
transfer lock on):

| Record | Value | Purpose |
|---|---|---|
| `suifleet.io` ALIAS/flattened CNAME | landing-site project | apex site |
| `www` CNAME | landing-site project, 301 to the apex | old links |
| `app` CNAME | console project | console, Testnet preview build until Mainnet |
| `docs` CNAME | documentation project | documentation |
| CAA | only the host's certificate authorities | limit certificate issuance |
| MX, SPF, DMARC (`p=reject` once verified) | mail forwarding for `hello@suifleet.io` | contact address on the site |

Before publishing, verify that `suifleet.io` is registered to the user's
account; this repository has not verified domain registration or ownership.
The contact page names `hello@suifleet.io`, so mail forwarding must deliver a
test message before the site goes live.

### Documentation domain

Phase 1: `docs.suifleet.io` redirects (host-level 301) to the repository's
`docs/` directory on GitHub, which the site's Documentation page already links
page by page. Phase 2 (not built here): render `docs/` to static HTML with the
same no-script policy and link checker. Report that work to Codex for triage
before starting it.

### Console domain

`app.suifleet.io` serves `dapp/out` built with the network's verified deployment
manifest (`NEXT_PUBLIC_DEPLOYMENT_MANIFEST`) and public configuration, following
[OPERATIONS.md](../docs/OPERATIONS.md#public-website-and-domains-sf-172). Until
Mainnet launches it must be the Testnet build, and the site says so. Google
sign-in needs the exact `https://app.suifleet.io/get-started/` redirect URI in
the OAuth client and Enoki app ([WALLET_ONBOARDING.md](../docs/WALLET_ONBOARDING.md)).

## Release process

Publication to a live domain needs the user's explicit authorization for that
release, recorded in the release issue. Building and checking do not.

1. On the reviewed main commit, run `make test-site site-check` (or take the CI
   result for that exact commit) and keep `site/dist` with its `SHA256SUMS`.
2. Review every page in a browser and play every screencast. Confirm guide
   steps still match the console that `app.suifleet.io` serves; re-record with
   `guide/record_guide.py` if the console changed.
3. Follow each external link once by hand; fix or remove dead ones.
4. With the user's authorization, upload exactly that `site/dist` to the
   landing-site project. Never edit files on the host.
5. Run `python3 site/verify_served.py site/dist https://suifleet.io` to confirm
   every served file matches `SHA256SUMS` and the security headers are present.
6. Record the commit, `SHA256SUMS` digest, host deployment ID, verification
   output and authorization in the release issue.

Rollback: re-upload the previously recorded `site/dist` (or use the host's
rollback to that deployment ID) and re-run step 5.
