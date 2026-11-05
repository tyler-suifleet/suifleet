# Independent customer hosting

The customer website, Testnet app and curated docs are deployed as public static
Sites. They run independently of this WSL machine. Their project IDs are retained
under `sites/`; native publication receipts are in `initial-deployments.json`.
Keep those IDs when updating. Never commit Site source credentials or wallet keys.

The requested final domains are `suifleet.io` / `www.suifleet.io`,
`app.suifleet.io`, and `docs.suifleet.io`. Their host-side bindings exist; domain
ownership and certificates remain pending until the owner updates Cloudflare DNS.
No Cloudflare DNS account is connected in this session, so no authoritative DNS
writes have been made. Google Workspace mail records must remain intact.

## One-time DNS connection

1. Open Cloudflare, choose **suifleet.io → DNS → Records → Import and Export**.
2. Review `dns-records.zone`, generated directly from the host's returned records.
   Import its web A/CNAME and ownership TXT records. It contains no MX, email
   authentication, nameserver or unrelated records. If `app` already exists,
   edit that web record to the supplied CNAME rather than leaving an A/CNAME
   conflict. Preserve all existing Google Workspace and unrelated records.
3. The supplied routing records explicitly select **DNS only**, so Cloudflare's
   optional import proxy checkbox cannot silently proxy them. Keep the provider
   verification TXT values exactly as returned.
4. Refresh each domain through the native custom-domain status tool. Require
   active ownership/routing and a valid HTTPS certificate, then verify actual
   website/app/docs navigation and media on the final domains.
5. Rebuild with `production-origins.json` and publish the verified source through
   the same Site projects. Until that cutover, `provisioning-origins.json` keeps
   all hosted crosslinks usable without advertising unresolved domains.

Cloudflare documents [zone-file import](https://developers.cloudflare.com/dns/manage-dns-records/how-to/import-and-export/)
and its per-record `cf-proxied:false` override. Do not migrate nameservers or
remove email records to attach this hosting.

## Build and update

```sh
PYTHONPATH=/path/to/hash-pinned/docs/packages python3 hosting/build.py \
  --origins hosting/provisioning-origins.json --out /private/new-hosted-release \
  --node /path/to/node-v22.22.2/bin/node
CHROME_EXECUTABLE=/path/to/chrome node hosting/test_hosted_browser.cjs \
  hosting/provisioning-origins.json
```

The builder validates the paired Testnet manifest before and after the pinned
frontend build, configures real public documentation links, prepares security
headers and emits source/configuration/file hashes. It never signs a transaction
or writes DNS. Review the output before publication. A stale UpgradeCap/profile
is a real error; resolve the paired manifest rather than weakening verification.

Use the Sites skill and its `site-workflow.mjs` helper for the existing projects.
Pass short-lived credentials only through hidden stdin. Keep the user's verified
Tyler Bush author/committer identity; make descriptive itemized snapshot commits
before invoking the helper so it does not create a generic commit. Package the
exact pushed source and use the audience-appropriate native save/deploy call.
The current customer sites are public. Do not change their audience on updates.

Physical Pi, manual wallet, production-provider/payment and Mainnet acceptance
are separate from successful static-host publication. The app remains Testnet.
