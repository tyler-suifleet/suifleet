# Customer audit implementation follow-through — 5 November 2026

The original [customer audit](CUSTOMER_EXPERIENCE_AUDIT.md) remains a dated
baseline. Its findings are addressed by source changes and evaluated deployments,
not merely renamed tickets. This table distinguishes those implementations from
owned-domain DNS and deferred production/hardware acceptance.

| Finding | Implemented behavior | Evidence / delivery |
|---|---|---|
| Capacity confused with operating funds | Device capacity terminology, explicit prerequisites, enabled checkout and separate balances. Visible test prepayment/application activates100 device places. | SF-244/246/248; current app and guide; SF-246/245 receipts. |
| Checkout unavailable in evaluation | Paired verified v4 profile has disposable SUI/USDC test offers, preserved subscriptions and registry. Clear test-tariff labeling; preparation/approval/confirmation progress beside the action. | SF-246/251; actual frontend checkout and native billing progress browser tests. |
| New fleets cannot configure reporting without a rebuild | App-wide reviewed storage/data release, authoritative per-fleet locators, administrator setup, enrollment and generated device settings. | SF-247/251; four fresh customers, actual native daemon samples, independent signatures/effects, unreported before reporting. Startup degraded is not healthy. |
| Confusing or empty storage/data pages | Locator-backed storage selection/discovery for new fleets, verified legacy fallback, missing records distinct from failed reads. | SF-251 production storage-reader evidence and current app source. Empty storage is not a claim of funded archive uploads. |
| Funding/upload/group workflow failures | Lossless native-coin wrapping; batch coin consumption; retryable storage reads preserving files; authoritative storage periods; managed group inventory independent of historic discovery. | SF-248/249/250; actual deposit, batch funding, publication, recovered group/member and Linux Applied receipt in SF-245. |
| Broken installation help | Curated installation/client portal; app connection settings and platform instructions linked to the configured public docs host. | SF-244/246/253; eight documentation tests and real anonymous HTTPS phone/desktop checks. |
| Security/contact channel not functional | GitHub private vulnerability reporting enabled and verified; working private reporting form and public preview feedback channel. Unverified future mailbox is not advertised as operational. | SF-244 audit evidence; current Security/Contact pages. No outside report was sent. |
| Network jargon and raw implementation details | Customer-first descriptions and names; installation internals in integrator disclosures; history diagnostics outside the main top-of-page flow. | SF-244 and prior UI implementations; all-route presentation/hydration checks. |
| Registration/publication/deployment/health conflated | Each lifecycle boundary explicitly named. Current health and recovered historical update failures remain separate. | Current dashboard/alerts, device/deployment views, guide and conceptual video. Actual device receipt independently verified. |
| Stale or inaccessible videos | Ten current female-narrated recordings, native captions, complete written alternative, verified install outcome, honest rollout creation/pause. | SF-245:53 tests,20 decoded files, full ASR,108 reviewed frames, seven viewports/all77 instructions; actual public HTTPS playback. |
| Missing conceptual introduction | Original116.95-second animated overview with landscape/portrait variants; wallet approval, records/files, outbound checks, verification, platform installation and separate reports. | SF-252:56 site tests, four decoded files, full audio ASR98.7%, actual responsive playback/fallback and transcript. |
| Misleading pricing/privacy/revocation claims | Graduated pricing explained, proposed commercial subscription separated from test funds, optional costs disclosed, no deletion/remote-erasure promise. Public metadata and retention limits explained. | SF-244; pricing generation consistency and customer copy tests. No commercial payment acceptance claimed. |
| Local-machine-dependent website/app/docs | Three public managed static deployments, immutable source/configuration/file hashes, no WSL origin required. Real HTTPS phone/desktop navigation checks pass. | SF-253; hosting project IDs, publication receipts and reproducible bundles under `hosting/`. |
| Owned domains not usable | Host-side bindings created for root/www/app/docs; exact provider-issued routing/verification records and DNS-only import file prepared. | **DNS activation still pending owner action:** no Cloudflare DNS credentials are connected. No authoritative DNS writes or owned-domain TLS success are claimed. |

The public website, app and documentation are hosted through the
[Sites plugin](plugin://sites@openai-curated-remote), with source provenance in
the deployment records. All commits use the user's verified Tyler Bush identity.
Temporary development tunnels may remain for comparison, but the managed hosts
do not use them as origins.

To complete the owned-domain cutover, follow [the concrete DNS instructions](../hosting/README.md),
verify ownership/TLS, and rebuild/redeploy the same projects with
`hosting/production-origins.json`. Existing Google Workspace MX/TXT/DKIM and
unrelated records remain unchanged. The prepared import contains only the listed
web routing and host-verification records.

Normal application review/CI remains required; publishing an evaluated Testnet
frontend is not a claim that every PR is merged. Physical Pi, manual wallet,
production payment/provider and Mainnet acceptance remain explicitly deferred
in SF-031/#32 and SF-087/#159. No hardware safety or paid-production readiness is
inferred from the diagrams, mocked authority tests or static publication.
