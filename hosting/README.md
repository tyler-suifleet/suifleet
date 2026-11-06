# Owner-controlled Cloudflare Pages hosting

The current provider is **Cloudflare Pages in the user's own account**. The
website, Testnet console and curated docs are independent static deployments.
`cloudflare-pages.json` pins the verified owner account/active zone and desired
project scope; `cloudflare-projects.json` records the actual provider-returned
project IDs and domains. Reuse these projects; do not create replacements on retry.

All four owned hostnames are active. The initial three-origin release is recorded
in `cloudflare-deployments.json` ([SF-256](../docs/validation/SF256.md)). The later
website/docs-only product-copy update is recorded in
`cloudflare-product-copy-deployments.json` ([SF-257](../docs/validation/SF257.md));
the console retains its original Testnet deployment. The subsequent website-only
fixed-layout overview video release is in
`cloudflare-overview-layout-deployment.json` (same SF-257 validation record).
The illustrated overview release develops actions around smaller fixed landmarks;
its evidence is in `cloudflare-illustrated-overview-deployment.json`. The latest
website-only professional narration revision is recorded in
`cloudflare-overview-narration-deployment.json`. The current connected-component
animation, which removes the detached corner legend, is recorded in
`cloudflare-connected-overview-deployment.json`. The user applied DNS manually; the model
performed no DNS-record writes.

| Address | Pages project |
|---|---|
| `suifleet.io`, `www.suifleet.io` | `suifleet-marketing` |
| `app.suifleet.io` | `suifleet-console` |
| `docs.suifleet.io` | `suifleet-docs` |

The domain's authoritative DNS was already on Cloudflare in this account. No
registrar/nameserver migration, Google hosting or paid plan is needed for this
static deployment. Google Workspace mail and unrelated DNS must remain intact.

## Authentication and DNS

Use the existing locked CLI, not an unpinned global install:

```sh
npm ci --prefix tools/static-hosting
WRANGLER_SEND_METRICS=false tools/static-hosting/node_modules/.bin/wrangler login \
  --device --browser=false --scopes account:read user:read pages:write zone:read
```

This browser/device login works from a phone and avoids WSL localhost callbacks.
Credentials remain in private Wrangler configuration; never print, copy into Git,
put into CLI arguments or reuse the personal OAuth token as a CI secret. Pages
access does not grant DNS-record editing. The actual DNS API refused this login
with403; no DNS mutation is claimed or attempted through that scope.

The Pages custom-domain associations are registered before DNS is pointed at
the projects. Their activation/TLS must be checked separately. Use the four
**actual provider-derived** CNAMEs in `cloudflare-dns.zone`; import/edit them in
Cloudflare DNS with Proxied enabled. Replace conflicting web records rather than
adding duplicates. Preserve MX/TXT/DKIM/CAA and all unrelated records.

**Do not import `dns-records.zone`: it is the obsolete OpenAI Sites plan.** The
old `sites/`, `domain-bindings.json` and Sites deployment receipts are historical
migration/rollback evidence, not the current deployment configuration. Old local
instructions were retired; the current file is `/home/tyler/suifleet-cloudflare-dns.zone`.
No OpenAI ownership TXT records are required by this hosting.

## Prepare, verify and publish

Run from a clean source checkout with pinned Node22.22.2 and the hash-pinned docs
requirements installed. `cloudflare-staging-origins.json` is for independent
Pages-host checks; `production-origins.json` is for the actual owned domains.

```sh
python3 hosting/cloudflare_pages.py provision --receipt /private/projects.json
python3 hosting/cloudflare_pages.py dns --receipt /private/projects.json --output /private/web-dns.zone
PYTHONPATH=/path/to/hash-pinned/docs/packages python3 hosting/build.py \
  --origins hosting/production-origins.json --dns-zone hosting/cloudflare-dns.zone \
  --out /private/new-immutable-bundles --node /path/to/node-v22.22.2/bin/node
```

Provisioning verifies zone/account ownership and existing project identity before
creating anything. Builds reverify the public Testnet profile before and after
export. DNS download inclusion requires an explicitly chosen provider file;
legacy Sites records are never silently added to a new bundle.

Use the existing `site/release.py seal` for marketing/docs/console and
`site/collection.py` to bind all three public-file inventories to one digest.
These gates enforce the static-only policy and Pages file/count/header limits.
Reverify that collection digest before upload; deploy exactly its public folders
with the pinned Wrangler to the recorded project/production branch, passing the
recorded source commit. Retain native deployment IDs, source and collection hashes.
Check served bytes/headers, mobile/desktop navigation and actual media playback.
Do not equate a successful upload with DNS/TLS or manual-wallet acceptance.

`cloudflare_pages.py bind --receipt /private/projects.json --output /private/domains.json`
creates missing domain associations idempotently. It does not edit DNS.

The existing `.github/workflows/static-site.yml` remains the protected future
release path: separate preview/production credentials, exact approved artifact,
main-only production environment and the user's reviewer gate. Machine OAuth
login does not configure or bypass those CI credential/approval protections.
This user-requested migration is a separately authorized, receipted local upload.

Official references: [Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/),
[custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/),
[DNS import](https://developers.cloudflare.com/dns/manage-dns-records/how-to/import-and-export/).

The separate 20-second Remotion/SVG sample is at
`https://suifleet.io/film-preview/`. Its release evidence is
`cloudflare-remotion-sample-deployment.json`. It is unlisted/noindex and does not
replace the full overview. Only marketing was deployed; app/docs are unchanged.

The latest sample at `/film-preview/` uses detailed generated artwork, clearer
handoffs and “Sui saves the approval” narration. Its current release evidence is
`cloudflare-detailed-film-deployment.json`; the full overview is still unchanged.
