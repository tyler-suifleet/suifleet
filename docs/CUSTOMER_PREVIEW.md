# Customer preview operations

This is the testnet evaluation preview, not a production deployment. The app is statically built; neither the website nor its HTTPS tunnel receives wallet keys. Dedicated testnet setup uses the existing bounded mutation tools and global network lease.

Current verified mobile entry points (temporary tunnel hostnames may change):

- Website: https://nothing-difference-look-color.trycloudflare.com/
- Guide: https://nothing-difference-look-color.trycloudflare.com/guide/
- App: https://characterized-above-chemical-possess.trycloudflare.com/
- Installation docs: https://persian-vessels-ended-structured.trycloudflare.com/device-client/

Do not give phone users localhost or a WSL private address. All three origins must be HTTPS and reachable. Build the app with `NEXT_PUBLIC_DEVICE_DOCS_BASE` set to the public documentation origin: the integrator help and onboarding page links share it. Build the website with a public configuration file containing only `origin`, `app_url` and `docs_url`:

```bash
python3 site/build.py --out /tmp/customer-site --config /path/to/public-origins.json
python3 site/check.py /tmp/customer-site --config /path/to/public-origins.json --require-videos
```

The builder rejects non-HTTPS, credential-bearing and private-IP overrides and includes the correct links/canonical origin in its hashed output. Preserve all narrated media and verify the final served file hashes. Compile the app from the explicit `profiles/seal-testnet/frontend.env` configuration and use frozen dependencies. Recheck chain/package/registry/type origins with `scripts/verify_profile_console.cjs` before promoting a release.

The committed evaluation profile contains public addresses and an explicitly disposable checkout tariff. Its nominal USD/conversion numbers are **not** a market rate or the commercial proposal. Checkout requires approval and uses test tokens; account network fees, subscription capacity and fleet operating funds remain separate. Prepayment creates/funds the subscription; applying the first due prepaid payment enables capacity. The two actual frontend builders and resulting 100-device capacity were tested with an isolated funded identity.

On this WSL host the app, landing site and documentation run in persistent user services, with leased ports 3000, 3004 and 3005. Their HTTPS tunnel services are respectively `suifleet-mobile-comparison.service`, `suifleet-landing-draft-https.service` and `suifleet-docs-preview-https.service`. Port holders assert/renew their coordinator leases and release them on stop. User services persist across the WSL user session; Windows/WSL must be running for temporary tunnels to work. Do not stop services or steal an existing port lease casually.

Artifacts are retained under `~/.local/state/suifleet/`; app startup checks its recorded file hashes and the website uses a release symlink. Public metadata identifies source commit/unmerged candidate and testnet profile. Promote a new verified artifact rather than editing live files; retain the preceding release for rollback. A metadata or build success does not prove live wallet or device acceptance.

After a compatible chain upgrade, regenerate **both** browser and device connection manifests, preserving introduction origins. Existing pinned clients reject a stale latest-package reference; distribute the new settings in a controlled rollout without changing their device key or journal. The SF-246 v3 upgrade preserved the existing registry and origins; physical Pi configuration rollout remains unclaimed.

Open work: SF-245/#612 refreshes videos/captions. SF-247/#616 implements self-service reporting/storage setup and dynamic discovery for new fleets; the retained configured evaluation budget alone does not establish that behavior. Production DNS/provider/credential changes and SF-173's separate Walrus Site publisher wallet remain outside this preview operation.
