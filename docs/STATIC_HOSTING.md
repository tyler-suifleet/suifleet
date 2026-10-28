# Static hosting preparation (SF-176; not deployed)

Selected provider: Cloudflare Pages Direct Upload for suifleet.io,
app.suifleet.io and docs.suifleet.io. No Functions, Workers, application server,
Walrus portal or wal.app redirect belongs on these origins. The Walrus Site
release remains a separate distribution. Registration remains at GoDaddy.

Selection checked 2026-10-27 against the provider's
[custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/),
[headers](https://developers.cloudflare.com/pages/configuration/headers/),
[Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/),
[limits](https://developers.cloudflare.com/pages/platform/limits/) and
[pricing](https://www.cloudflare.com/plans/developer-platform/) documentation.
Budget: Pages Free ($0 hosting base fee), subject to provider limits; registrar,
mail forwarding and optional paid services are separate. No purchase is authorized.
Free allows 20,000 files/site, each at most 25 MiB. Screencasts must respect that
limit or require a separately reviewed distribution plan. No analytics, Zaraz,
Rocket Loader, HTML rewriting or automatic script injection may be enabled.
Pages reads `_headers` for static responses; a CSP meta tag alone cannot enforce
frame-ancestors or HSTS. The marketing build supplies the reviewed
`site/src/_headers`. Docs must supply its own reviewed policy; the console needs
a policy compatible with Next inline script hashes and its configured network
endpoints. Never copy the script-free marketing CSP onto the console blindly.

## Current state and completion gates

The workflow now builds and seals all three origins: the SF-172 marketing source,
the committed SF-177 docs builder (3844c2a9), and the console at the workflow
revision using the reviewed `profiles/seal-testnet` deployment profile. The console
receipt binds the revision and manifest digest; it explicitly records manual
recording acceptance as unrun. This is a source release candidate, not evidence of
a recorded real-wallet release. Production authorization must also state
`Recorded console release accepted` after that acceptance actually occurs.
SF-172 and SF-177 remain upstream merge/review prerequisites.

`site/prepare_hosted.py` copies outputs to new directories. It removes the private
docs build marker, adds docs HSTS, and supplies console discovery files and CSP
hashes for the actual Next inline scripts. It validates the deployment manifest,
rejects external script sources and removes Walrus routing metadata only from the
conventional-host copy. The Walrus distribution is unchanged. The console policy
is deliberately restricted to the testnet profile; new provider/wallet endpoints
require reviewed policy and browser failure-path checks before enabling them.

No provider account/projects, credentials, registrar ownership, TLS certificates,
real preview publication or hosted rollback has been verified. These external
provider checks remain pending in SF-176/#353; final real-wallet/provider/mainnet
acceptance stays in #32/#159. Offline A→B→A fixture restoration is a regression
check only, never a provider deployment or rollback receipt. Source completion
does not authorize provisioning credentials or DNS/production publication.

Before release, provision isolated preview projects through an authorized operator,
record real three-origin preview URLs/deployment IDs and digests, check served
bytes/headers/TLS and rehearse preview rollback. Obtain independent review and
exact-head CI; obtain explicit production authorization only after those checks.

## Account and workflow setup

Create Direct Upload projects with these names and production branches:

| Artifact | Production project / branch | Isolated preview project / branch |
| --- | --- | --- |
| marketing | suifleet-marketing / production | suifleet-marketing-preview / preview |
| exact console export | suifleet-console / production | suifleet-console-preview / preview |
| SF-177 docs | suifleet-docs / production | suifleet-docs-preview / preview |

These are desired names, **not verified reservations or DNS targets**. Confirm
availability before configuring. Use separate Cloudflare accounts for preview and
production so preview credentials cannot publish production. The token needs
Account / Cloudflare Pages / Edit for only its account. Keep tokens solely in the
corresponding GitHub environment secret `CLOUDFLARE_API_TOKEN`; set its account ID
as environment variable `CLOUDFLARE_ACCOUNT_ID`. No DNS-edit token is needed in CI.
The workflow pins Node 22.22.2 and the lockfile pins Wrangler and transitive
package integrity.

Configure `static-preview` to allow only main. Configure `static-production` to
allow only main and require the user's `therealtime` account as reviewer; the
workflow checks this reviewer list before exposing production secrets. Disable
admin bypass where supported. Do not auto-approve this environment or provide
production credentials to preview/build jobs. A dispatch is a request; only
Tyler's explicit environment approval authorizes the actual publication. The
input authorization comment must be written by `therealtime` in #353 and contain
`Authorize production publication`, the full revision and SHA256.
A comment URL or a model starting the workflow does not grant authorization.

`static-site.yml` runs offline tests, builds all three origins, uploads
`static-release`, and records the collection.json SHA256. Each collection entry
binds one release.json digest, which binds every public file. Every main push attempts
the stable preview publication; absent configuration is a failure, not acceptance.
Production is dispatch-only on main, requires that exact digest and the authorization
comment URL in #353. It rebuilds deterministically and refuses a changed source
revision/artifact. There is no tag/push production trigger or provider-side rebuild.
Publishing all three origins is not atomic: complete the preview compatibility
check before each approved release, and retain previous bundles for each origin.

## DNS and TLS change plan (requires authorization)

1. Export the existing GoDaddy zone, including MX/TXT/CAA and DNSSEC DS. Preserve
   mail records. Add the zone in the user's Cloudflare account and record its
   **actual assigned** nameservers; change GoDaddy NS only after reviewing the zone.
   Do not guess nameserver values. Apex Pages requires Cloudflare authoritative DNS.
2. Handle DNSSEC migration with the registrar/provider procedure; remove stale DS
   before switching signed zones and publish the new verified DS after activation.
3. Add each custom domain in Pages **before** creating its DNS record. Record
   dashboard-returned targets and certificate status, then use:

   | Name | Intended record / target, subject to provider verification |
   | --- | --- |
   | @ | flattened CNAME to suifleet-marketing.pages.dev |
   | app | CNAME to suifleet-console.pages.dev |
   | docs | CNAME to suifleet-docs.pages.dev |
   | www | CNAME to marketing project, with an authorized permanent apex redirect |

   Record actual DNS answers/TTL and custom domain activation in the issue. Avoid
   broad redirect rules affecting app/docs. Stable previews use
   `preview.<preview-project>.pages.dev`, never quick tunnels; custom preview
   hostnames may be added only through the same authorized domain process.
4. Review existing CAA restrictions. Allow only the issuers required by the actual
   Pages/Universal SSL certificate configuration, using the provider's
   [CAA guidance](https://developers.cloudflare.com/ssl/edge-certificates/caa-records/).
   Record actual `issue`/`issuewild` values and certificate issuer/SAN/expiry rather
   than inventing a fixed CA. Test issuance and renewal readiness before launch.
5. Require managed HTTPS and HTTP-to-HTTPS redirects, TLS 1.2 minimum, certificate
   SANs matching every origin. HSTS is max-age=31536000 at each origin; do not add
   includeSubDomains/preload until all subdomains are audited. Verify HTTP status,
   HTTPS bytes, headers, redirects and certificate chain externally.

## Bundle checks and rollback rehearsal

Build marketing without network:

```sh
python3 -m unittest discover -s site/tests -v
python3 site/build.py --out /tmp/marketing-a
python3 site/build.py --out /tmp/marketing-b
diff -r /tmp/marketing-a /tmp/marketing-b
python3 site/check.py /tmp/marketing-a
python3 site/release.py seal /tmp/marketing-a /tmp/marketing-release \
  --revision "$(git rev-parse HEAD)" --hostname suifleet.io
python3 site/release.py verify /tmp/marketing-release --sha256 APPROVED_RELEASE_JSON_SHA256
```

The release digest binds all static bytes, headers and revision. Keep it outside
`public/`. Extra/missing/modified files, symlinks, worker entrypoints, hidden files,
private file extensions and provider file/size violations are rejected. These
checks are defense in depth, not a general secret scanner. Run repository security
checks and review all public configuration before upload.

For the authorized preview rehearsal, retain two sealed bundles A and B. Verify
and deploy A, record the Wrangler deployment ID/URL and served-byte check; deploy
B and verify; re-upload A to the same preview branch and verify A again. Use the
locked CLI (`npm ci --ignore-scripts --prefix tools/static-hosting`), not npx fetching
an unpinned version. Never use a production project for the rehearsal. Record all
three deployment IDs, times, artifact digests and outcomes. Production rollback
requires the user's authorization, restores the retained approved artifact without
edits/rebuild, repeats served-byte/TLS/header verification and records its outcome.
The workflow archives upload output even when verification fails; a failed served
check means the deployment may have happened and requires reconciliation, not a
blind retry. No hosted rollback rehearsal has run yet; the offline fixture is explicitly separate.

## Wallet screening and on-chain identity

Use stable TLS origins and consistent SuiFleet title, description, icon, canonical
and Open Graph metadata. Before mainnet launch, open the scanner provider's
verified review/support channel through Slush's official support, supply the owned
origins, security/contact pages, source/release revision, exact preview URL, and
alert ID `058f67f4-9057-429a-b798-b73cf0bafe11`. Record the case and provider response;
do not claim an allowlist entry or promise a clean scan. Perform actual Slush
connect-time screening later under [SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32)
and [SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159), including
network mismatch, signing and error paths. Do not ask users to bypass warnings.

`suifleet.sui` is the planned official on-chain identity, **not yet verified
registered**. Tyler Bush is responsible for registration and renewal. The
SuinsRegistration NFT must be held by the user's organization wallet, never an
agent/CI wallet. After the user registers it, record the public organization
address, NFT ID, transaction, expiry and verified current maximum renewal horizon;
update security/contact pages to call it official only then. Do not advertise an
unregistered name as proof of identity. The user renews before expiry (calendar
reminders at 60/30/7 days), may renew ahead up to the current on-chain maximum, and
has a 30-day grace period after expiry per the ticket's 2026-10-27 verified chain
read. Recheck current contract/UI limits before renewal. Agents never buy, renew
or reconfigure the name. Publisher/treasury target use and a possible
suifleet.wal.app mirror need separate authorization. No mainnet transaction or
production-provider/manual-wallet acceptance is claimed here.
